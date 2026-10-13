"""
Excel 模板下载 + 批量导入（spec V1.3 §B.2 + §B.3 + §9）

对外暴露：
- build_template_xlsx() -> bytes                    # §B.3：6 必需列 + 1 行示例
- build_template_xlsx_with_rows(rows) -> bytes       # 测试用：构造合法 xlsx
- build_xlsx_with_columns(cols, rows) -> bytes       # 测试用：自定义列（含缺列）
- parse_import_xlsx_bytes(xlsx) -> ImportPlan        # §B.2：解析 + 单行校验
- import_rows(conn, plan, project_resolver,
              test_item_resolver=None,
              overwrite=False) -> dict               # §B.2 g/h：入库 + 合并扫描

错误语义：
- 缺必需列 → M.MissingRequiredColumns（整批拒绝，不写库）
- 项目不存在 → X.ProjectNotFound（整批拒绝）
- 检测指标不存在 → X.TestItemNotFound（整批拒绝）
- 重复记录（未传 overwrite）→ M.DuplicateImportRows

设计要点：
- 纯函数 + openpyxl，不接 DB 之外的事务管理（DB 部分在 import_rows 内）
- parse 阶段只做"格式校验"，不接 DB；"项目/指标是否存在"由 caller 提供
  project_resolver / test_item_resolver 函数，调用方用 projects.list_projects / 
  list_test_items 实现；这样 parse 阶段在测试里完全可 mock。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from . import measurements as M
from . import westgard as W


# ============================================================================
# 模板列定义
# ============================================================================

TEMPLATE_COLUMNS: list[str] = [
    "项目名", "检测指标", "水平", "批号", "日期", "测定值",
]
# 也允许带"序号"列（用于同日重复测定）
TEMPLATE_COLUMNS_WITH_SEQ: list[str] = TEMPLATE_COLUMNS + ["序号"]


@dataclass
class ImportRow:
    """解析后的单条数据"""
    project_name: str
    test_item_name: str
    level: str
    lot: str
    date: str  # 'YYYY-MM-DD'
    value: Any
    seq: int = 0
    original_row_no: int = 0  # 在 xlsx 中的行号（含表头 = 1）


@dataclass
class SkippedRow:
    """被跳过的行 + 错误原因（写错误报告）"""
    original_row_no: int
    reason: str

    def line_report(self) -> str:
        """返回单行错误报告文本（用户友好格式）。"""
        return f"  第 {self.original_row_no} 行：{self.reason}"


def export_error_report(plan: ImportPlan, *, save_to: str | None = None) -> str:
    """导出错误报告（spec §B.2 边界：错误报告要能导出成文件）。

    把 plan.skipped_rows 转成用户可读的文本报告（行号 + 错误原因），
    用户照着行号改完 Excel 再重新导入。

    参数：
        plan：parse_import_xlsx_bytes 的返回结果
        save_to：若提供路径，则写入文件；否则只返回文本

    返回：报告全文（str）

    示例输出：
        ==================== 导入错误报告 ====================
        总览：成功 48 行，跳过 2 行

        跳过的行：
          第 3 行：日期格式错误
          第 7 行：测定值非数字
        ==================== 导入错误报告 ====================
    """
    lines: list[str] = [
        "======================== 导入错误报告 ========================",
        f"总览：成功 {len(plan.ok_rows)} 行，跳过 {len(plan.skipped_rows)} 行",
        "",
        "跳过的行：",
    ]
    if plan.skipped_rows:
        for sr in sorted(plan.skipped_rows, key=lambda s: s.original_row_no):
            lines.append(sr.line_report())
    else:
        lines.append("  （无跳过行）")
    lines.append("=" * 60)
    text = "\n".join(lines)
    if save_to:
        Path(save_to).write_text(text, encoding="utf-8")
    return text


@dataclass
class ImportPlan:
    columns: list[str] = field(default_factory=list)
    ok_rows: list[ImportRow] = field(default_factory=list)
    skipped_rows: list[SkippedRow] = field(default_factory=list)


# ============================================================================
# 自定义业务异常
# ============================================================================

class ProjectNotFound(Exception):
    """spec §B.2 边界：项目不存在 → 整批拒绝"""

    def __init__(self, name: str):
        super().__init__(f"项目 {name} 不存在，请先创建项目")
        self.name = name


class TestItemNotFound(Exception):
    """spec §B.2 边界：项目存在但检测指标不存在 → 整批拒绝"""

    def __init__(self, name: str, project_name: str):
        super().__init__(
            f"检测指标 {name} 不存在（项目 {project_name}），请先创建检测指标"
        )
        self.name = name
        self.project_name = project_name


# ============================================================================
# 模板构建（§B.3）
# ============================================================================

def build_template_xlsx() -> bytes:
    """§B.3：6 必需列 + 1 行示例"""
    sample = [{
        "项目名": "维生素 D",
        "检测指标": "D2",
        "水平": "L",
        "批号": "LOT1",
        "日期": "2026-04-01",
        "测定值": 100.0,
        "序号": 1,
    }]
    return build_template_xlsx_with_rows(sample)


def build_template_xlsx_with_rows(
    rows: Iterable[dict],
    *,
    columns: Optional[Sequence[str]] = None,
) -> bytes:
    """测试 / 内部用：构造一个带表头 + 指定行的合法 xlsx。"""
    cols = list(columns) if columns is not None else list(TEMPLATE_COLUMNS_WITH_SEQ)
    wb = Workbook()
    ws = wb.active
    ws.title = "质控数据"
    ws.append(cols)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDDDDD")
    for r in rows:
        ws.append([r.get(c, None) for c in cols])
    import io
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_xlsx_with_columns(
    columns: Sequence[str],
    rows: Iterable[dict],
) -> bytes:
    """测试用：构造任意列的 xlsx（可故意缺列）。"""
    return build_template_xlsx_with_rows(rows, columns=columns)


# ============================================================================
# 解析（§B.2 解析阶段，不接 DB）
# ============================================================================

def parse_import_xlsx_bytes(xlsx_bytes: bytes) -> ImportPlan:
    """解析上传的 xlsx，返回 ImportPlan。

    必填列：项目名 / 检测指标 / 水平 / 批号 / 日期 / 测定值（6 列）。
    可选列：序号（缺则按行号顺序自动给 0,1,2,...）。
    """
    from openpyxl import load_workbook
    wb = load_workbook(io_bytes:=_BytesIO(xlsx_bytes), data_only=True, read_only=False)
    ws = wb.active

    # 1. 读表头
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    header = [str(h).strip() if h is not None else "" for h in header_row]
    wb.close()

    # 2. 缺列校验（整批拒绝）
    missing = [c for c in TEMPLATE_COLUMNS if c not in header]
    if missing:
        raise M.MissingRequiredColumns(missing)

    # 3. 列索引
    idx = {c: header.index(c) for c in header}

    # 4. 解析每一行（重新打开 + read_only 模式）
    wb2 = load_workbook(io_bytes2:=_BytesIO(xlsx_bytes), data_only=True, read_only=True)
    ws2 = wb2.active

    plan = ImportPlan(columns=header)
    seq_counter = 0
    for ridx, row in enumerate(ws2.iter_rows(min_row=2, values_only=True), start=2):
        if row is None:
            continue
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
            continue  # 空行跳过
        original_row_no = ridx
        # 必备字段读取
        try:
            project_name = str(row[idx["项目名"]]).strip() \
                if row[idx["项目名"]] is not None else ""
            test_item_name = str(row[idx["检测指标"]]).strip() \
                if row[idx["检测指标"]] is not None else ""
            level = str(row[idx["水平"]]).strip() \
                if row[idx["水平"]] is not None else ""
            lot = str(row[idx["批号"]]).strip() \
                if row[idx["批号"]] is not None else ""
            date_raw = row[idx["日期"]]
            value_raw = row[idx["测定值"]]
            seq_raw = row[idx["序号"]] if "序号" in idx else None
        except (IndexError, KeyError):
            plan.skipped_rows.append(SkippedRow(
                original_row_no=original_row_no,
                reason=f"第 {original_row_no} 行数据列缺失",
            ))
            continue

        # 必填字段非空
        if not project_name or not test_item_name or not level or not lot or date_raw is None:
            plan.skipped_rows.append(SkippedRow(
                original_row_no=original_row_no,
                reason=f"第 {original_row_no} 行必填字段为空",
            ))
            continue

        # 日期格式校验
        date_iso = _coerce_date(date_raw)
        if date_iso is None:
            plan.skipped_rows.append(SkippedRow(
                original_row_no=original_row_no,
                reason=f"第 {original_row_no} 行日期格式错误",
            ))
            continue

        # 测定值校验（非数字/空白 → 跳过）
        if value_raw is None or (isinstance(value_raw, str) and not value_raw.strip()):
            plan.skipped_rows.append(SkippedRow(
                original_row_no=original_row_no,
                reason=f"第 {original_row_no} 行测定值为空白",
            ))
            continue
        # 若 value_raw 是字符串但能转 float，继续；不能转的跳过
        if isinstance(value_raw, str) and not _is_numeric(value_raw):
            plan.skipped_rows.append(SkippedRow(
                original_row_no=original_row_no,
                reason=f"第 {original_row_no} 行测定值非数字",
            ))
            continue

        # 序号
        if seq_raw is None or seq_raw == "":
            seq = seq_counter
        else:
            try:
                seq = int(seq_raw)
            except (TypeError, ValueError):
                seq = seq_counter
        seq_counter += 1

        # 水平 ∈ {L, H}
        if level not in ("L", "H"):
            plan.skipped_rows.append(SkippedRow(
                original_row_no=original_row_no,
                reason=f"第 {original_row_no} 行水平必须为 L 或 H",
            ))
            continue

        plan.ok_rows.append(ImportRow(
            project_name=project_name,
            test_item_name=test_item_name,
            level=level,
            lot=lot,
            date=date_iso,
            value=value_raw,
            seq=seq,
            original_row_no=original_row_no,
        ))
    wb2.close()
    return plan


def _coerce_date(raw: Any) -> Optional[str]:
    """只支持 'YYYY-MM-DD' 格式（spec §B.2 边界表 + §9 隐含约定）。
    其他格式（如 YYYY/MM/DD、YYYY.MM.DD、Excel 自动 datetime 等）都视为错误。
    """
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, str):
        s = raw.strip()
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


# ============================================================================
# 入库（§B.2 h：入库 + 逐条判定 + 触发保存时合并扫描）
# ============================================================================

def import_rows(
    conn: sqlite3.Connection,
    plan: ImportPlan,
    *,
    project_resolver: Callable[[str], Optional[int]],
    test_item_resolver: Optional[Callable[[str, str, str], Optional[int]]] = None,
    overwrite: bool = False,
) -> dict:
    """把 plan.ok_rows 入库。

    参数:
      project_resolver(name) -> Optional[int]
          项目名 → project_id；返回 None 表示"项目不存在"，整批拒绝。
      test_item_resolver(name, project_name, level) -> Optional[int]
          检测指标名 + 项目名 + 水平 → test_item_id；返回 None 表示"指标不存在"。
          若为 None，则用默认实现：list_test_items(conn, project_id, name=name) 
          取第一个匹配 level 的 test_item。
      overwrite: 重复时是否覆盖。默认 False → 抛 DuplicateImportRows。

    流程（spec §B.2 h）：
      1. 项目解析（任一行项目不存在 → 整批拒绝）
      2. 检测指标解析（任一行指标不存在 → 整批拒绝）
      3. 查重（test_item_id + lot + level + date + seq）→ 不 overwrite 则抛 DuplicateImportRows
      4. 逐行 create_measurement（每行触发保存时合并扫描）
      5. 合并扫描"去重"：导入完成后，对本次涉及的 (test_item_id, date) 各扫一次（防重复）
    """
    from . import projects as P

    # 1. 解析所有项目 ID
    project_id_cache: dict[str, int] = {}
    test_item_id_cache: dict[tuple[str, str, str], int] = {}

    rows = plan.ok_rows
    if not rows:
        return {"imported_count": 0, "skipped_count": len(plan.skipped_rows),
                "duplicates": 0, "rescanned_keys": []}

    # 1+2. 一次性解析所有（项目，检测指标）组合
    for r in rows:
        if r.project_name not in project_id_cache:
            pid = project_resolver(r.project_name)
            if pid is None:
                raise ProjectNotFound(r.project_name)
            project_id_cache[r.project_name] = pid
        pid = project_id_cache[r.project_name]

        key = (r.test_item_name, r.project_name, r.level)
        if key not in test_item_id_cache:
            if test_item_resolver is not None:
                tid = test_item_resolver(r.test_item_name, r.project_name, r.level)
            else:
                # 默认实现：list_test_items 找匹配 level 的第一个
                items = P.list_test_items(conn, pid, name=r.test_item_name)
                tid = next((it.id for it in items if it.level == r.level), None)
            if tid is None:
                raise TestItemNotFound(r.test_item_name, r.project_name)
            test_item_id_cache[key] = tid

    # 3. 查重
    rows_with_ids = [
        {**{
            "test_item_id": test_item_id_cache[(r.test_item_name, r.project_name, r.level)],
            "lot": r.lot, "level": r.level, "date": r.date, "seq": r.seq,
        }}
        for r in rows
    ]
    dup_indexes = M.find_duplicates_in_import(conn, rows_with_ids)
    if dup_indexes and not overwrite:
        raise M.DuplicateImportRows(len(dup_indexes))

    # overwrite=True → 先删旧的
    if dup_indexes and overwrite:
        for i in dup_indexes:
            r = rows_with_ids[i]
            conn.execute(
                "DELETE FROM measurements "
                "WHERE test_item_id = ? AND lot = ? AND level = ? "
                "AND date = ? AND seq = ?",
                (r["test_item_id"], r["lot"], r["level"], r["date"], r["seq"]),
            )

    # 4. 逐行入库（每行触发保存时合并扫描）
    # 为了避免对同一 (test_item, date) 重复扫多次，先用 _skip_rescan 跳过单行触发，
    # 然后最后对本次涉及的 (test_item_id, date) 组合各扫一次
    rescan_keys: set[tuple[int, str]] = set()
    imported_count = 0
    for r in rows:
        tid = test_item_id_cache[(r.test_item_name, r.project_name, r.level)]
        pid = project_id_cache[r.project_name]
        M.create_measurement(
            conn,
            test_item_id=tid,
            project_id=pid,
            lot=r.lot, level=r.level,
            date=r.date,
            value=r.value,
            seq=r.seq,
            _skip_rescan=True,
        )
        rescan_keys.add((tid, r.date))
        imported_count += 1

    # 5. 对每个 (test_item_id, date) 触发一次保存时合并扫描
    for tid, date_iso in rescan_keys:
        M.rescan_day_for_test_item(conn, test_item_id=tid, date=date_iso)

    return {
        "imported_count": imported_count,
        "skipped_count": len(plan.skipped_rows),
        "duplicates": len(dup_indexes),
        "rescanned_keys": sorted(rescan_keys),
    }


# helper: openpyxl read_only 需要 file-like
def _BytesIO(b: bytes):
    import io
    return io.BytesIO(b)


__all__ = [
    # 异常
    "ProjectNotFound", "TestItemNotFound",
    # 数据
    "ImportRow", "SkippedRow", "ImportPlan",
    # 模板
    "build_template_xlsx", "build_template_xlsx_with_rows",
    "build_xlsx_with_columns", "TEMPLATE_COLUMNS",
    # 解析
    "parse_import_xlsx_bytes",
    # 入库
    "import_rows",
    # 错误报告导出
    "export_error_report",
]