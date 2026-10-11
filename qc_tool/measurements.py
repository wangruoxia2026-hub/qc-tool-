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

    流程：
      1. 读 test_item（取 project_id/name/lot/target/sd/level）
      2. 找共享 (project_id, name, lot) 的所有 test_item（按 level 索引 target/sd）
      3. 取当日所有水平的所有非作废 measurements
      4. 对每个水平取"该水平在该日之前"的历史 values
      5. 用 westgard.evaluate_day 处理跨水平规则（用各水平的 target/sd）
      6. 用 westgard.evaluate_measurement 处理单水平规则（用各水平的 target/sd）
      7. 写回 status / rules；触发失控的 → INSERT INTO out_of_control
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

    # 3. 读当日所有非作废 measurements（所有水平）
    today_rows = conn.execute(
        "SELECT * FROM measurements "
        "WHERE test_item_id IN ({}) AND date = ? "
        "AND (status IS NULL OR status != '作废') "
        "ORDER BY level, seq, id".format(
            ",".join("?" for _ in level_target_sd)
        ),
        tuple([tid for (tid, _, _) in level_target_sd.values()] + [date]),
    ).fetchall()

    by_level: dict[str, list[sqlite3.Row]] = {lvl: [] for lvl in level_target_sd}
    for r in today_rows:
        by_level[r["level"]].append(r)

    # 4. 对每个水平取历史 values（"在该日之前"的非作废行）
    history_by_level: dict[str, list[float]] = {lvl: [] for lvl in level_target_sd}
    for lvl, (tid, _t, _s) in level_target_sd.items():
        hist_rows = conn.execute(
            "SELECT value FROM measurements "
            "WHERE test_item_id = ? AND date < ? "
            "AND (status IS NULL OR status != '作废') "
            "ORDER BY date, seq, id",
            (tid, date),
        ).fetchall()
        history_by_level[lvl] = [float(r["value"]) for r in hist_rows]

    # 5. 跨水平规则（evaluate_day）取当日各水平最后一条
    last_value_by_level: dict[str, Optional[float]] = {}
    for lvl in level_target_sd:
        rows = by_level[lvl]
        last_value_by_level[lvl] = float(rows[-1]["value"]) if rows else None

    def _td(lvl: str) -> tuple[float, float]:
        _, t, s = level_target_sd[lvl]
        return t, s

    has_any = (
        any(last_value_by_level[lvl] is not None for lvl in level_target_sd)
    )
    if has_any:
        # evaluate_day 需要 8 个位置参数（L/H 都给）
        lt, ls = _td("L") if "L" in level_target_sd else (0.0, 1.0)
        ht, hs = _td("H") if "H" in level_target_sd else (0.0, 1.0)
        day_res = W.evaluate_day(
            last_value_by_level.get("L"),
            lt, ls,
            last_value_by_level.get("H"),
            ht, hs,
            history_by_level.get("L", []),
            history_by_level.get("H", []),
        )
    else:
        day_res = {
            "low_status": W.Status.IN_CONTROL, "low_rules": [],
            "high_status": W.Status.IN_CONTROL, "high_rules": [],
        }

    # 6. 写回当日每条 measurement 的 status / rules
    rows_updated = 0
    ooc_inserted = 0
    for lvl in level_target_sd:
        # 该水平在 evaluate_day 中的跨水平规则结果
        day_key = "low" if lvl == "L" else "high"
        day_rules = day_res.get(f"{day_key}_rules", [])
        day_status_enum = day_res.get(f"{day_key}_status", W.Status.IN_CONTROL)

        tid, target, sd = level_target_sd[lvl]
        same_level_before = list(history_by_level[lvl])
        for r in by_level[lvl]:
            # 单条判定：用"该水平在该日之前的全部历史 + 当日该水平的之前 seq"
            history_for_this = list(same_level_before)
            res = W.evaluate_measurement(
                value=float(r["value"]),
                target=target, sd=sd,
                history=history_for_this,
            )
            # 把"该水平当日之前的 measurement"也并入 history（顺序追加）
            same_level_before.append(float(r["value"]))

            # 跨水平规则也并入（去重）
            merged_rules = sorted(set(res["rules"]) | set(day_rules))
            merged_status = (
                W.Status.OUT_OF_CONTROL if (
                    res["status"] == W.Status.OUT_OF_CONTROL or
                    day_status_enum == W.Status.OUT_OF_CONTROL
                ) else (
                    W.Status.WARNING if (
                        res["status"] == W.Status.WARNING or
                        day_status_enum == W.Status.WARNING
                    ) else W.Status.IN_CONTROL
                )
            )
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

            # 7. 触发失控的 → INSERT INTO out_of_control
            if merged_status == W.Status.OUT_OF_CONTROL:
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

    return {
        "rows_updated": rows_updated,
        "ooc_inserted": ooc_inserted,
        "low_status": day_res["low_status"],
        "high_status": day_res["high_status"],
        "low_rules": day_res["low_rules"],
        "high_rules": day_res["high_rules"],
    }


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
    "update_measurement_value", "mark_void",
    # 扫描
    "rescan_day_for_test_item",
    # 导入查重
    "find_duplicates_in_import",
]