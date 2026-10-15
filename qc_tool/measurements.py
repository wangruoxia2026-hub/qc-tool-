"""
功能 B：测定数据录入（spec V1.3 §B.1 + §B.4）

业务层 API（被 GUI / tests / 后续 issue 调用）：

异常类
-------
- ValueMustNonNegative         弹"必须 ≥ 0"
- ValueInvalidNumber           弹"请输入数字"
- DateInFuture                 弹"日期晚于今天，请确认"
- DateTooOld                   弹"日期较久"
- DuplicateImportRows          弹"发现 N 条重复，是否覆盖"
- MissingRequiredColumns       弹"缺少必需列：XXX"

数据类
------
- Measurement

CRUD + 校验 + 合并扫描
----------------------
- validate_measurement_value(value)        # 校验值（≥0，非数字抛 ValueInvalidNumber）
- validate_measurement_date(date_str)     # 校验日期（返回是否未来 / 是否 >2 年前）
- create_measurement(conn, ...)            # 入库 + 触发保存时合并扫描
- get_measurement(conn, mid)
- list_measurements(conn, test_item_id)    # 含作废行（GUI 上层过滤）
- list_measurements_for_day(conn, test_item_id, lot, level, date)
- update_measurement_value(conn, mid, value)   # 编辑值 + 触发保存时合并扫描
- mark_void(conn, mid)                      # 标记作废 status='作废'
- find_duplicates_in_import(conn, rows)     # 导入前查重（返回 list of row_no）
- rescan_day_for_test_item(conn, test_item_id, date)
    # 保存时合并扫描核心：取该检测指标 + 该日所有水平的所有非作废 measurements
    # 调 westgard.evaluate_day / evaluate_measurement 写回 status / rules
    # 触发失控的 → INSERT INTO out_of_control

所有"保存"动作（create / update）必须在同一事务内完成 + 触发保存时合并扫描。
跨日补录：扫描 SQL 限定 `WHERE date=?`，不触及其他日期。

不依赖 PyQt6：本模块纯 sqlite3 + westgard.py。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from . import westgard as W


# ============================================================================
# 自定义业务异常
# ============================================================================

class ValueMustNonNegative(ValueError):
    """spec §9 / §B.1：测定值为负数 → 弹'必须 ≥ 0'"""

    def __init__(self, msg: str = "必须 ≥ 0"):
        super().__init__(msg)


class ValueInvalidNumber(ValueError):
    """spec §9：测定值填文字 → 弹'请输入数字'"""

    def __init__(self, msg: str = "请输入数字"):
        super().__init__(msg)


class DateInFuture(ValueError):
    """spec §9：日期晚于今天 → 弹'日期晚于今天，请确认'"""

    def __init__(self, msg: str = "日期晚于今天，请确认"):
        super().__init__(msg)


class DateTooOld(ValueError):
    """spec §9：日期早于当前日期 2 年以上 → 提示'日期较久'（允许保存）"""

    def __init__(self, msg: str = "日期较久"):
        super().__init__(msg)


class DuplicateImportRows(ValueError):
    """spec §B.2 边界：重复记录弹'发现 N 条重复，是否覆盖'"""

    def __init__(self, count: int, msg: Optional[str] = None):
        if msg is None:
            msg = f"发现 {count} 条重复，是否覆盖"
        super().__init__(msg)
        self.count = count


class MissingRequiredColumns(ValueError):
    """spec §B.2 b：模板缺必需列 → 弹'缺少必需列：XXX'"""

    def __init__(self, columns: Iterable[str]):
        cols = list(columns)
        super().__init__("缺少必需列：" + ", ".join(cols))
        self.columns = cols


# ============================================================================
# 数据类
# ============================================================================

@dataclass
class Measurement:
    id: int
    test_item_id: int
    project_id: int
    lot: str
    level: str
    date: str  # 'YYYY-MM-DD'
    seq: int
    value: float
    status: Optional[str]  # '在控' / '警告(1_2s)' / '失控(规则)' / '作废' / None
    rules: Optional[str]    # JSON 数组字符串
    operator: Optional[str]
    pre_processor: Optional[str]
    note: Optional[str]
    created_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Measurement":
        return cls(
            id=row["id"],
            test_item_id=row["test_item_id"],
            project_id=row["project_id"],
            lot=row["lot"],
            level=row["level"],
            date=row["date"],
            seq=row["seq"],
            value=row["value"],
            status=row["status"],
            rules=row["rules"],
            operator=row["operator"],
            pre_processor=row["pre_processor"],
            note=row["note"],
            created_at=row["created_at"],
        )


# ============================================================================
# 校验（不接 DB）
# ============================================================================

def validate_measurement_value(value: Any) -> float:
    """spec §9：测定值校验。

    - 非数字（文字 / 空 / 空白 / True/False）→ ValueInvalidNumber
    - < 0 → ValueMustNonNegative（"必须 ≥ 0"）
    - 0 → 允许
    - 返回 float 化的值
    """
    if isinstance(value, bool):
        raise ValueInvalidNumber("请输入数字")
    if value is None:
        raise ValueInvalidNumber("请输入数字")
    # 空字符串 / 全空白
    if isinstance(value, str) and not value.strip():
        raise ValueInvalidNumber("请输入数字")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueInvalidNumber("请输入数字")
    if v < 0:
        raise ValueMustNonNegative("必须 ≥ 0")
    return v


def validate_measurement_date(date_str: str, *, today: Optional[date] = None) -> dict:
    """spec §9：日期校验。

    返回 dict：
      - 'date': date 对象
      - 'is_future': bool  → True 时 GUI 弹"日期晚于今天，请确认"
      - 'is_too_old': bool → True 时 GUI 提示"日期较久"（仍允许保存）
      - 'iso': 'YYYY-MM-DD'

    不抛异常（业务上"未来"是警告不是错误；"太早"也只是提示）。
    """
    today = today or date.today()
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueInvalidNumber("请输入日期，格式 YYYY-MM-DD")

    iso = d.isoformat()
    return {
        "date": d,
        "iso": iso,
        "is_future": d > today,
        "is_too_old": (today.year - d.year) > 2,
    }


# ============================================================================
# CRUD
# ============================================================================

def create_measurement(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    project_id: int,
    lot: str,
    level: str,
    date: str,
    value: float,
    seq: int = 0,
    operator: Optional[str] = None,
    pre_processor: Optional[str] = None,
    note: Optional[str] = None,
    _skip_rescan: bool = False,
) -> Measurement:
    """新建一条 measurement + 触发保存时合并扫描。

    同事务：若 rescan 阶段写 out_of_control 失败，全部回滚。
    """
    cleaned_value = validate_measurement_value(value)
    iso_date = validate_measurement_date(date)["iso"]
    created_at = _now_iso()

    cur = conn.execute(
        "INSERT INTO measurements("
        "  test_item_id, project_id, lot, level, date, seq, value,"
        "  status, rules, operator, pre_processor, note, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
        (
            test_item_id, project_id, lot, level, iso_date, seq,
            cleaned_value,
            operator, pre_processor, note, created_at,
        ),
    )
    mid = int(cur.lastrowid)

    if not _skip_rescan:
        # 保存时合并扫描：扫描该检测指标 + 该日所有水平
        rescan_day_for_test_item(conn, test_item_id=test_item_id, date=iso_date)

    row = conn.execute("SELECT * FROM measurements WHERE id = ?", (mid,)).fetchone()
    return Measurement.from_row(row)


def get_measurement(conn: sqlite3.Connection, mid: int) -> Optional[Measurement]:
    row = conn.execute("SELECT * FROM measurements WHERE id = ?", (mid,)).fetchone()
    return Measurement.from_row(row) if row else None


def list_measurements(
    conn: sqlite3.Connection,
    test_item_id: int,
    *,
    include_void: bool = True,
) -> list[Measurement]:
    """列某检测指标的所有 measurements（按 date asc, level asc, seq asc）。

    include_void=True 时返回作废行（GUI 上层自己灰显 + 删除线）。
    """
    if include_void:
        rows = conn.execute(
            "SELECT * FROM measurements WHERE test_item_id = ? "
            "ORDER BY date, level, seq, id",
            (test_item_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM measurements WHERE test_item_id = ? "
            "AND (status IS NULL OR status != '作废') "
            "ORDER BY date, level, seq, id",
            (test_item_id,),
        ).fetchall()
    return [Measurement.from_row(r) for r in rows]


def list_measurements_for_day(
    conn: sqlite3.Connection,
    test_item_id: int,
    date: str,
    *,
    include_void: bool = False,
) -> list[Measurement]:
    """列某检测指标某日的所有 measurements（含两水平 + 多 seq）。"""
    if include_void:
        rows = conn.execute(
            "SELECT * FROM measurements WHERE test_item_id = ? AND date = ? "
            "ORDER BY level, seq, id",
            (test_item_id, date),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM measurements WHERE test_item_id = ? AND date = ? "
            "AND (status IS NULL OR status != '作废') "
            "ORDER BY level, seq, id",
            (test_item_id, date),
        ).fetchall()
    return [Measurement.from_row(r) for r in rows]


def update_measurement_value(
    conn: sqlite3.Connection,
    mid: int,
    *,
    value: float,
    _skip_rescan: bool = False,
) -> Measurement:
    """编辑 measurement 的 value + 触发保存时合并扫描（同 §B.4 c）。"""
    cleaned_value = validate_measurement_value(value)
    existing = get_measurement(conn, mid)
    if existing is None:
        raise LookupError(f"measurement {mid} 不存在")
    if existing.status == "作废":
        raise ValueError("已作废的记录不可编辑")

    conn.execute(
        "UPDATE measurements SET value = ? WHERE id = ?",
        (cleaned_value, mid),
    )

    if not _skip_rescan:
        rescan_day_for_test_item(
            conn, test_item_id=existing.test_item_id, date=existing.date,
        )

    row = conn.execute("SELECT * FROM measurements WHERE id = ?", (mid,)).fetchone()
    return Measurement.from_row(row)


def update_measurement_fields(
    conn: sqlite3.Connection,
    mid: int,
    *,
    date: Optional[str] = None,
    level: Optional[str] = None,
    lot: Optional[str] = None,
    seq: Optional[int] = None,
    value: Optional[float] = None,
    _skip_rescan: bool = False,
) -> Measurement:
    """spec §B.4 a — 编辑一条 measurement 的 5 字段（任意子集）+ 触发保存时合并扫描。

    字段全部可选 — 调用方只传要改的字段。
    改 date/level/lot/seq 后必须重扫：跨水平规则可能改变（spec §D.4 d/e）。
    改 value 后也必须重扫（§D 整套规则）。
    已作废记录不可编辑。
    """
    existing = get_measurement(conn, mid)
    if existing is None:
        raise LookupError(f"measurement {mid} 不存在")
    if existing.status == "作废":
        raise ValueError("已作废的记录不可编辑")

    # 1) 字段校验（按需）
    new_date = existing.date
    if date is not None:
        new_date = validate_measurement_date(date)["iso"]
    new_level = existing.level
    if level is not None:
        if level not in ("L", "H"):
            raise ValueError(f"level 必须为 'L' 或 'H'，实得 {level!r}")
        new_level = level
    new_lot = existing.lot
    if lot is not None:
        s = str(lot).strip()
        if not s:
            raise ValueError("lot 不能为空")
        new_lot = s
    new_seq = existing.seq
    if seq is not None:
        try:
            new_seq = int(seq)
        except (TypeError, ValueError):
            raise ValueInvalidNumber("seq 必须为整数")
        if new_seq < 0:
            raise ValueMustNonNegative("seq 必须 ≥ 0")
    new_value = existing.value
    if value is not None:
        new_value = validate_measurement_value(value)

    # 2) 写回
    conn.execute(
        "UPDATE measurements SET date = ?, level = ?, lot = ?, seq = ?, value = ? "
        "WHERE id = ?",
        (new_date, new_level, new_lot, new_seq, new_value, mid),
    )

    # 3) 保存时合并扫描 — 用新 date（可能改了 date）
    # 注意：spec §B.1 f — 仅扫该 (test_item_id, date)；如果旧日期也有同指标
    # 数据，原本的判定可能仍正确，但保守起见扫新 date（变更日）。
    if not _skip_rescan:
        rescan_day_for_test_item(
            conn, test_item_id=existing.test_item_id, date=new_date,
        )
        # 如果改了 date，**旧日期**的扫描结果也需重跑（因为同日扫描结果可能变）
        if new_date != existing.date:
            rescan_day_for_test_item(
                conn, test_item_id=existing.test_item_id, date=existing.date,
            )

    row = conn.execute("SELECT * FROM measurements WHERE id = ?", (mid,)).fetchone()
    return Measurement.from_row(row)


def mark_void(conn: sqlite3.Connection, mid: int) -> Measurement:
    """标记作废：spec §B.4 d — status='作废'，不参与判定 / 不在质控图显示。"""
    existing = get_measurement(conn, mid)
    if existing is None:
        raise LookupError(f"measurement {mid} 不存在")
    if existing.status == "作废":
        return existing  # 幂等
    conn.execute(
        "UPDATE measurements SET status = '作废', rules = NULL WHERE id = ?",
        (mid,),
    )
    row = conn.execute("SELECT * FROM measurements WHERE id = ?", (mid,)).fetchone()
    return Measurement.from_row(row)


# ============================================================================
# 保存时合并扫描（核心）
# ============================================================================

def rescan_day_for_test_item(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    date: str,
) -> dict:
    """spec §B.1 f：保存时合并扫描。

    扫描范围：与该 test_item 共享 (project_id, name, lot) 的所有 level 的
    所有非作废 measurements 在该 date 的数据；每个水平各自的历史独立。
    跨日补录：仅扫该 date，不扫其他日期。

    跨水平规则需要"同 name 同日另一 level"的 measurements（spec §A.2：每个
    检测指标独立配 2 个水平 L/H —— DB 上是两个 test_item 行）。判定单元
    四维（spec §D.3）= 检测指标 + 批号 + 水平 + 日期；共享 (project_id, name)
    视为同一检测指标的不同水平。

    规则按用户 2026-10-13 裁定的规则表执行（tests/test_rescan_seq_pair.py）：
      - 同水平规则（1_2s / 1_3s / 2_2s① / 4_1s① / 10_x①）沿该水平自身值序列；
        2_2s①（两条均失控）/ 4_1s①（第 4 条）/ 10_x①（第 10 条）保留跨日累计；
      - R_4s 同水平线：同日相邻两条一超 +2SD 一超 −2SD 且极差 >= 4SD → 两条均失控；
      - 跨水平规则（2_2s② / R_4s / 4_1s② / 10_x②）按「同日期 + 同序号」配对
        （westgard.evaluate_day_by_seq），只标参与判定的点，不连坐当天其他行；
      - 2_2s① 跨日对：早一天（或晚一天）那条一并回写补标。

    流程：
      1. 读 test_item（取 project_id/name/lot/target/sd/level）
      2. 找共享 (project_id, name, lot) 的所有 test_item（按 level 索引 target/sd）
      3. 取当日所有水平的所有非作废 measurements（按 level, seq, id 排序）
      4. 取各水平该日之前的历史（含 id，供跨日对回写）+ 该日之后第一条
      5. 跨水平规则按「同日期 + 同序号」配对判定
      6. 逐行写回 status / rules；触发失控的 → INSERT INTO out_of_control
    """
    # 1. 读 test_item
    ti_row = conn.execute(
        "SELECT t.*, p.name AS project_name FROM test_items t "
        "JOIN projects p ON p.id = t.project_id "
        "WHERE t.id = ?",
        (test_item_id,),
    ).fetchone()
    if ti_row is None:
        raise LookupError(f"test_item {test_item_id} 不存在")
    project_id = ti_row["project_id"]
    test_item_name = ti_row["name"]
    project_name = ti_row["project_name"]
    lot = ti_row["lot"]

    # 2. 找共享 (project_id, name, lot) 的所有 test_item（按 level 索引 target/sd）
    sibling_rows = conn.execute(
        "SELECT id, target, sd, lot, level FROM test_items "
        "WHERE project_id = ? AND name = ? AND lot = ?",
        (project_id, test_item_name, lot),
    ).fetchall()
    level_target_sd: dict[str, tuple[int, float, float]] = {}
    for r in sibling_rows:
        level_target_sd[r["level"]] = (int(r["id"]), float(r["target"]), float(r["sd"]))

    # 3. 读当日所有非作废 measurements（所有水平，按 level, seq, id 排序）
    today_rows = conn.execute(
        "SELECT * FROM measurements "
        "WHERE test_item_id IN ({}) AND date = ? "
        "AND (status IS NULL OR status != '作废') "
        "ORDER BY level, seq, id".format(
            ",".join("?" for _ in level_target_sd)
        ),
        tuple([tid for (tid, _, _) in level_target_sd.values()] + [date]),
    ).fetchall()

    ordered_by_level: dict[str, list[sqlite3.Row]] = {lvl: [] for lvl in level_target_sd}
    by_seq: dict[str, dict[int, sqlite3.Row]] = {lvl: {} for lvl in level_target_sd}
    for r in today_rows:
        ordered_by_level[r["level"]].append(r)
        # 同 seq 多条时配对取最后一条（正常流程查重后不会出现，仅防御）
        by_seq[r["level"]][r["seq"]] = r

    # 4. 各水平历史（该日之前）+ 该日之后第一条（跨日 2_2s① 对回写用）
    history_by_level: dict[str, list[tuple[int, float]]] = {lvl: [] for lvl in level_target_sd}
    next_first_by_level: dict[str, Optional[tuple[int, float]]] = {lvl: None for lvl in level_target_sd}
    for lvl, (tid, _t, _s) in level_target_sd.items():
        hist_rows = conn.execute(
            "SELECT id, value FROM measurements "
            "WHERE test_item_id = ? AND date < ? "
            "AND (status IS NULL OR status != '作废') "
            "ORDER BY date, seq, id",
            (tid, date),
        ).fetchall()
        history_by_level[lvl] = [(int(r["id"]), float(r["value"])) for r in hist_rows]
        nx = conn.execute(
            "SELECT id, value FROM measurements "
            "WHERE test_item_id = ? AND date > ? "
            "AND (status IS NULL OR status != '作废') "
            "ORDER BY date, seq, id LIMIT 1",
            (tid, date),
        ).fetchone()
        if nx is not None:
            next_first_by_level[lvl] = (int(nx["id"]), float(nx["value"]))

    # 5. 跨水平规则：同日期 + 同序号配对
    cross: dict[str, dict[int, list[str]]] = {"low": {}, "high": {}}
    if "L" in by_seq and "H" in by_seq:
        lt, ls = level_target_sd["L"][1], level_target_sd["L"][2]
        ht, hs = level_target_sd["H"][1], level_target_sd["H"][2]
        cross = W.evaluate_day_by_seq(
            {s: float(r["value"]) for s, r in by_seq["L"].items()}, lt, ls,
            {s: float(r["value"]) for s, r in by_seq["H"].items()}, ht, hs,
        )

    # 6. 逐水平逐行判定 + 写回
    rows_updated = 0
    ooc_inserted = 0
    agg: dict[str, dict] = {
        lvl: {"rules": set(), "status": W.Status.IN_CONTROL} for lvl in level_target_sd
    }
    for lvl, (tid, target, sd) in level_target_sd.items():
        rows = ordered_by_level[lvl]
        day_key = "low" if lvl == "L" else "high"
        cross_for_level = cross.get(day_key, {})
        full_hist = [v for (_i, v) in history_by_level[lvl]]

        # 6.1 同水平完整序列（历史最后一条 → 当日 → 该日之后第一条）做 2_2s① 配对：
        #     两条同方向 > 2SD → 两条均失控（含跨日对的回写）
        chain: list[tuple[str, int, float]] = []
        if history_by_level[lvl]:
            hid, hval = history_by_level[lvl][-1]
            chain.append(("hist", hid, hval))
        for i, r in enumerate(rows):
            chain.append(("day", i, float(r["value"])))
        if next_first_by_level[lvl] is not None:
            nid, nval = next_first_by_level[lvl]
            chain.append(("next", nid, nval))

        pair_flags = [False] * len(rows)
        retro_ids: set[int] = set()
        for a, b in zip(chain, chain[1:]):
            if W.check_2_2s([a[2], b[2]], target, sd):
                for node in (a, b):
                    if node[0] == "day":
                        pair_flags[node[1]] = True
                    else:
                        retro_ids.add(node[1])

        # 6.2 R_4s 同水平线：仅同日相邻两条（一正一负 + 极差 >= 4SD）→ 两条均失控
        r4s_flags = [False] * len(rows)
        for i in range(1, len(rows)):
            if W.check_r_4s_same_level(
                float(rows[i - 1]["value"]), float(rows[i]["value"]), target, sd,
            ):
                r4s_flags[i - 1] = True
                r4s_flags[i] = True

        # 6.3 每个 seq 当日最后一条（跨水平规则只挂到参与配对的这一条）
        last_idx_for_seq: dict[int, int] = {}
        for i, r in enumerate(rows):
            last_idx_for_seq[int(r["seq"])] = i

        # 6.4 逐行判定写回
        same_level_before: list[float] = list(full_hist)
        for i, r in enumerate(rows):
            value = float(r["value"])
            row_rules: list[str] = []
            if W.check_1_2s(value, target, sd):
                row_rules.append("1_2s")
            if W.check_1_3s(value, target, sd):
                row_rules.append("1_3s")
            if pair_flags[i]:
                row_rules.append("2_2s")
            if r4s_flags[i]:
                row_rules.append("R_4s")
            full = list(same_level_before)
            if W.check_4_1s(full + [value], target, sd):
                row_rules.append("4_1s")
            if W.check_10_x(full + [value], target, sd):
                row_rules.append("10_x")
            if last_idx_for_seq.get(int(r["seq"])) == i:
                row_rules.extend(cross_for_level.get(int(r["seq"]), []))
            same_level_before.append(value)

            merged_rules = sorted(set(row_rules))
            if any(x in W.OUT_OF_CONTROL_RULES for x in merged_rules):
                merged_status = W.Status.OUT_OF_CONTROL
            elif merged_rules:
                merged_status = W.Status.WARNING
            else:
                merged_status = W.Status.IN_CONTROL

            # 在控时 rules 写 NULL（不存空数组）；警告/失控时存 JSON
            if merged_status == W.Status.IN_CONTROL:
                rules_str = None
            else:
                rules_str = json.dumps(merged_rules, ensure_ascii=False)
            status_text = _status_text(merged_status, merged_rules)
            conn.execute(
                "UPDATE measurements SET status = ?, rules = ? WHERE id = ?",
                (status_text, rules_str, r["id"]),
            )
            rows_updated += 1

            agg[lvl]["rules"] |= set(merged_rules)
            if merged_status == W.Status.OUT_OF_CONTROL:
                agg[lvl]["status"] = W.Status.OUT_OF_CONTROL
                ooc_inserted += _insert_out_of_control_if_new(
                    conn,
                    measurement_id=int(r["id"]),
                    test_item_id=tid,
                    project_id=project_id,
                    lot=r["lot"],
                    level=r["level"],
                    triggered_date=r["date"],
                    test_item_name=test_item_name,
                    project_name=project_name,
                    rules=merged_rules,
                )
            elif merged_status == W.Status.WARNING and \
                    agg[lvl]["status"] == W.Status.IN_CONTROL:
                agg[lvl]["status"] = W.Status.WARNING

        # 6.5 跨日 2_2s① 对回写（只增不减；该行自己那天的 rescan 会全量重算）
        for mid in sorted(retro_ids):
            ooc_inserted += _retro_add_2_2s(
                conn, mid, project_name=project_name, test_item_name=test_item_name,
            )
            rows_updated += 1

    # 7. 汇总（兼容旧返回结构：low/high 为各水平当日全部触发的规则并集 + 最差状态）
    result: dict[str, Any] = {
        "rows_updated": rows_updated,
        "ooc_inserted": ooc_inserted,
    }
    for lvl in level_target_sd:
        key = "low" if lvl == "L" else "high"
        result[f"{key}_status"] = agg[lvl]["status"]
        result[f"{key}_rules"] = sorted(agg[lvl]["rules"])
    return result


def _retro_add_2_2s(
    conn: sqlite3.Connection,
    measurement_id: int,
    *,
    project_name: str,
    test_item_name: str,
) -> int:
    """2_2s① 跨日对回写：给非当日那条合并 2_2s 规则并更新状态 / 补 OOC 记录。

    只增不减：该行自己那天的 rescan 会全量重算它自己的规则。
    """
    row = conn.execute(
        "SELECT * FROM measurements WHERE id = ?", (measurement_id,),
    ).fetchone()
    if row is None or row["status"] == "作废":
        return 0
    rules = json.loads(row["rules"]) if row["rules"] else []
    if "2_2s" in rules:
        return 0
    rules = sorted(set(rules) | {"2_2s"})
    if any(x in W.OUT_OF_CONTROL_RULES for x in rules):
        status = W.Status.OUT_OF_CONTROL
    elif rules:
        status = W.Status.WARNING
    else:
        status = W.Status.IN_CONTROL
    conn.execute(
        "UPDATE measurements SET status = ?, rules = ? WHERE id = ?",
        (_status_text(status, rules), json.dumps(rules, ensure_ascii=False), measurement_id),
    )
    if status == W.Status.OUT_OF_CONTROL:
        return _insert_out_of_control_if_new(
            conn,
            measurement_id=measurement_id,
            test_item_id=row["test_item_id"],
            project_id=row["project_id"],
            lot=row["lot"],
            level=row["level"],
            triggered_date=row["date"],
            test_item_name=test_item_name,
            project_name=project_name,
            rules=rules,
        )
    return 0


def _insert_out_of_control_if_new(
    conn: sqlite3.Connection,
    *,
    measurement_id: int,
    test_item_id: int,
    project_id: int,
    lot: str,
    level: str,
    triggered_date: str,
    test_item_name: str,
    project_name: str,
    rules: list[str],
) -> int:
    """同一条 measurement + rules 组合只生成一条 out_of_control（去重）。"""
    existing = conn.execute(
        "SELECT 1 FROM out_of_control "
        "WHERE measurement_id = ? AND rules = ? LIMIT 1",
        (measurement_id, json.dumps(rules, ensure_ascii=False)),
    ).fetchone()
    if existing is not None:
        return 0
    conn.execute(
        "INSERT INTO out_of_control("
        "  measurement_id, test_item_id, project_id,"
        "  lot, level, triggered_date,"
        "  test_item_name, project_name, rules"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            measurement_id, test_item_id, project_id,
            lot, level, triggered_date,
            test_item_name, project_name,
            json.dumps(rules, ensure_ascii=False),
        ),
    )
    return 1


def _status_text(status_enum, rules: list[str]) -> str:
    """把 Status 枚举渲染成 spec §B.1 状态列字串。"""
    if status_enum == W.Status.OUT_OF_CONTROL:
        if not rules:
            return "失控"
        return "失控(" + " + ".join(rules) + ")"
    if status_enum == W.Status.WARNING:
        if not rules:
            return "警告"
        return "警告(" + " + ".join(rules) + ")"
    return "在控"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================================
# 导入前查重
# ============================================================================

def find_duplicates_in_import(
    conn: sqlite3.Connection,
    rows: Sequence[dict],
) -> list[int]:
    """spec §B.2 g：导入前查重。

    rows 每项含：test_item_id, lot, level, date, seq
    返回已存在于 DB 的行（按 rows 索引顺序）。
    """
    dup_indexes: list[int] = []
    for idx, r in enumerate(rows):
        existing = conn.execute(
            "SELECT 1 FROM measurements "
            "WHERE test_item_id = ? AND lot = ? AND level = ? "
            "AND date = ? AND seq = ? LIMIT 1",
            (r["test_item_id"], r["lot"], r["level"], r["date"], r["seq"]),
        ).fetchone()
        if existing is not None:
            dup_indexes.append(idx)
    return dup_indexes


# ============================================================================
# 公共入口：保存完一条 measurement 后该调这个函数
# ============================================================================
#
# spec V1.3 §B.1 f："保存时立即合并扫描" — 调用方（手工录入 / Excel 导入 /
# 第三方脚本）保存完一条 measurement 后必须立即调本函数，触发该 (test_item_id,
# date) 组合的"保存时合并扫描"（跨水平规则 + 重算 status / rules）。
#
# 实现内部直接复用 rescan_day_for_test_item（同一份代码、零开销别名）。
# 不重写、不分支——保证和 create_measurement / update_measurement_value 走的
# 是同一份判定路径。
#
# 调用示例：
#     from qc_tool.measurements import on_measurement_saved
#     cur.execute("INSERT INTO measurements(...) VALUES (...)")
#     on_measurement_saved(conn, test_item_id=..., date=...)
#
# 测试用：
#     from qc_tool.measurements import on_measurement_saved
#     on_measurement_saved(conn, test_item_id=1, date="2026-04-01")
#
on_measurement_saved = rescan_day_for_test_item


__all__ = [
    # 异常
    "ValueMustNonNegative", "ValueInvalidNumber",
    "DateInFuture", "DateTooOld",
    "DuplicateImportRows", "MissingRequiredColumns",
    # 数据类
    "Measurement",
    # 校验
    "validate_measurement_value", "validate_measurement_date",
    # CRUD
    "create_measurement", "get_measurement",
    "list_measurements", "list_measurements_for_day",
    "update_measurement_value", "update_measurement_fields", "mark_void",
    # 扫描（公共入口）
    "on_measurement_saved", "rescan_day_for_test_item",
    # 导入查重
    "find_duplicates_in_import",
]