"""
功能 G：月度质控报表 / 失控记录导出 / 质控图 PNG 导出（spec V1.3 §G.1-G.3）

对外 API
--------
G.1 月报
- select_test_items_for_month(conn, project_name, year, month)
      -> list[TestItemIndicator]              # 弹出"选择导出指标"对话框的源数据
- monthly_has_data(conn, project_name, year, month) -> bool
- build_monthly_report_xlsx(conn, project_name, year, month, selected_test_item_ids)
      -> bytes                                # 多 sheet xlsx，每勾选指标 1 组 × 3 sheet

G.2 失控记录导出
- has_ooc_in_range(conn, project_name, date_from, date_to_inclusive) -> bool
- build_ooc_report_xlsx(conn, project_name, date_from, date_to_inclusive) -> bytes
- build_ooc_report_pdf(conn, project_name, date_from, date_to_inclusive) -> bytes

异常
- NoIndicatorSelected：月报时未勾选任何指标
- ProjectNotFound：项目名找不到

设计要点（spec V1.3 §G + AGENTS.md 铁律）：
1. **纯读取** — 不修改任何业务表（measurements / out_of_control / test_items / projects）
2. **复用 §C 统计** — 累计统计走 statistics.compute_cumulative_stats 的区间约定
   （建库日 ~ 上一自然月最后一天，不含当月）
3. **多 sheet 结构** — 每指标 1 组 3 sheet：本月原始 / 累计统计 / 去失控累计
4. **去失控定义** — 按用户决议：排除 status='OutOfControl*' 和 status='Void'，
   保留 'InControl' 和 'Warning*'（与 §B.4 d"作废不参与统计"一致）
5. **PDF 矢量** — reportlab Canvas 矢量绘制；字号 ≥ 10pt（spec G.2 b 验收）
6. **失控字段** — 按用户决议：out_of_control 表全字段（14 列）
7. **月界** — 半开区间 [date_from, date_to_inclusive]（caller 决定；本模块不归一化）

不在本期范围（spec §10）：多用户、自定义规则、跨水平对比图、打印预览等
"""
from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# reportlab imported lazily in build_ooc_report_pdf to keep base module cheap
from . import statistics as S


# ============================================================================
# 数据类 & 异常
# ============================================================================

@dataclass
class TestItemIndicator:
    """'选择导出指标'对话框的一行（G.1 a）。

    label 形如 'D2-低' / 'D2-高'（spec 验证示例）。
    """
    test_item_id: int
    name: str
    level: str
    lot: str
    label: str


class NoIndicatorSelected(ValueError):
    """G.1 c：未勾选任何指标。"""

    def __init__(self, msg: str = "请至少勾选一个指标"):
        super().__init__(msg)


class ProjectNotFound(LookupError):
    """项目名在 projects 表中找不到。"""

    def __init__(self, name: str = ""):
        super().__init__(f"项目不存在: {name!r}" if name else "项目不存在")
        self.name = name


# ============================================================================
# G.1 公共查询
# ============================================================================

def _resolve_project(conn: sqlite3.Connection, project_name: str) -> int:
    """返回 project_id；不存在抛 ProjectNotFound。"""
    row = conn.execute(
        "SELECT id FROM projects WHERE name = ?", (project_name,)
    ).fetchone()
    if row is None:
        raise ProjectNotFound(project_name)
    return int(row["id"])


_LEVEL_DISPLAY = {"L": "低", "H": "高"}


def select_test_items_for_month(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    year: int,
    month: int,
) -> list[TestItemIndicator]:
    """返回该项目下所有「检测指标 + 水平」组合（spec G.1 a 弹窗源数据）。

    注意：spec G.1 a 验收示例为「维生素 D2-低、维生素 D2-高、维生素 D3-低、
    维生素 D3-高 共 4 个指标可勾选」—— 即"每个 test_item 行 = 1 个导出指标"，
    不按 level 折叠。label 中文形如 'D2-低'（L→低 / H→高）。

    项目名不存在 → 返回空列表（caller 自行决定弹什么；测试期望"无项目"=空）。
    """
    pid_row = conn.execute(
        "SELECT id FROM projects WHERE name = ?", (project_name,)
    ).fetchone()
    if pid_row is None:
        return []
    pid = int(pid_row["id"])
    rows = conn.execute(
        "SELECT id, name, level, lot FROM test_items "
        "WHERE project_id = ? ORDER BY name, level",
        (pid,),
    ).fetchall()
    return [
        TestItemIndicator(
            test_item_id=int(r["id"]),
            name=str(r["name"]),
            level=str(r["level"]),
            lot=str(r["lot"]),
            label=f"{r['name']}-{_LEVEL_DISPLAY.get(r['level'], r['level'])}",
        )
        for r in rows
    ]


def monthly_has_data(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    year: int,
    month: int,
) -> bool:
    """G.1 b：月内无数据 → 提示'无数据'。caller 先调用此函数决定是否生成报表。

    项目不存在 → 返回 False（caller 拿不到"无项目"和"无数据"区分；建议
    build_monthly_report_xlsx 用 try/except ProjectNotFound 在 UI 层处理）。
    """
    pid_row = conn.execute(
        "SELECT id FROM projects WHERE name = ?", (project_name,)
    ).fetchone()
    if pid_row is None:
        return False
    pid = int(pid_row["id"])
    last_day = _month_last_day(year, month)
    row = conn.execute(
        "SELECT 1 FROM measurements "
        "WHERE project_id = ? "
        "  AND date >= ? AND date <= ? "
        "LIMIT 1",
        (pid, f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"),
    ).fetchone()
    return row is not None


# ============================================================================
# G.1 月报主入口
# ============================================================================

# Sheet 名称模板
_SHEET1_NAME = "{label} - 本月原始"
_SHEET2_NAME = "{label} - 累计统计"
_SHEET3_NAME = "{label} - 去失控累计"
_SHEET4_NAME = "{label} - 质控图"
# Excel sheet 名最长 31 字符
_SHEET_NAME_MAX = 31

# 当数据为空时填入的占位文本
_NO_DATA_TEXT = "无数据"

# 表头
_HEADER_BOLD = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="305496")
_CENTER = Alignment(horizontal="center", vertical="center")


def _month_last_day(year: int, month: int) -> int:
    from calendar import monthrange
    return monthrange(year, month)[1]


def _truncate_sheet_name(name: str) -> str:
    return name[:_SHEET_NAME_MAX]


# 表头
def _fetch_month_measurements(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    year: int,
    month: int,
) -> list[sqlite3.Row]:
    last = _month_last_day(year, month)
    return conn.execute(
        "SELECT date, seq, value, status, rules, operator, pre_processor "
        "FROM measurements "
        "WHERE test_item_id = ? "
        "  AND date >= ? AND date <= ? "
        "ORDER BY date, seq, id",
        (test_item_id, f"{year:04d}-{month:02d}-01",
         f"{year:04d}-{month:02d}-{last:02d}"),
    ).fetchall()


def _fill_header_row(ws, headers: list[str]) -> None:
    for c in ws[1]:
        c.font = _HEADER_BOLD
        c.fill = _HEADER_FILL
        c.alignment = _CENTER
    # set column widths
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(h) * 2)


def _write_sheet1_本月原始(
    wb: Workbook,
    conn: sqlite3.Connection,
    *,
    label: str,
    project_name: str,
    test_item_name: str,
    level: str,
    lot: str,
    test_item_id: int,
    year: int,
    month: int,
) -> None:
    """G.1 sheet 1: 本月原始（全部测定值 + 状态）。"""
    sheet_name = _truncate_sheet_name(_SHEET1_NAME.format(label=label))
    ws = wb.create_sheet(sheet_name)
    headers = ["日期", "序号", "测定值", "状态", "触发规则", "操作员", "前处理"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=h)
    _fill_header_row(ws, headers)
    # 元信息
    ws.cell(row=1, column=len(headers) + 2, value="项目")
    ws.cell(row=1, column=len(headers) + 3, value=project_name)
    ws.cell(row=2, column=len(headers) + 2, value="检测指标")
    ws.cell(row=2, column=len(headers) + 3, value=f"{test_item_name} - {level}")
    ws.cell(row=3, column=len(headers) + 2, value="批号")
    ws.cell(row=3, column=len(headers) + 3, value=lot)
    ws.cell(row=4, column=len(headers) + 2, value="月份")
    ws.cell(row=4, column=len(headers) + 3, value=f"{year:04d}-{month:02d}")
    # 数据
    rows = _fetch_month_measurements(
        conn, test_item_id=test_item_id, year=year, month=month,
    )
    if not rows:
        ws.cell(row=2, column=1, value=_NO_DATA_TEXT)
        return
    for r_idx, r in enumerate(rows, start=2):
        ws.cell(row=r_idx, column=1, value=r["date"])
        ws.cell(row=r_idx, column=2, value=int(r["seq"]))
        ws.cell(row=r_idx, column=3, value=float(r["value"]))
        ws.cell(row=r_idx, column=4, value=r["status"] or "")
        # rules: JSON list → "1_3s,2_2s"
        rules_raw = r["rules"]
        rules_str = ""
        if rules_raw:
            try:
                parsed = json.loads(rules_raw)
                if isinstance(parsed, list):
                    rules_str = ",".join(str(x) for x in parsed)
            except (TypeError, ValueError):
                rules_str = str(rules_raw)
        ws.cell(row=r_idx, column=5, value=rules_str)
        ws.cell(row=r_idx, column=6, value=r["operator"] or "")
        ws.cell(row=r_idx, column=7, value=r["pre_processor"] or "")


def _write_sheet2_累计统计(
    wb: Workbook,
    conn: sqlite3.Connection,
    *,
    label: str,
    test_item_id: int,
    lot: str,
    level: str,
) -> None:
    """G.1 sheet 2: 累计统计（第一条质控数据 ~ 上一自然月最后一天，含失控点）。"""
    sheet_name = _truncate_sheet_name(_SHEET2_NAME.format(label=label))
    ws = wb.create_sheet(sheet_name)
    headers = ["N", "均值", "SD", "CV%", "区间说明"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=h)
    _fill_header_row(ws, headers)
    # 复用 §C 累计统计；sheet2 保持"含失控"语义 → 显式 exclude_ooc=False
    stats = S.compute_cumulative_stats(
        conn, test_item_id=test_item_id, lot=lot, level=level,
        exclude_ooc=False,
    )
    # 区间说明
    interval = _describe_cumulative_range(conn, test_item_id, lot, level)
    ws.cell(row=2, column=5, value=interval)
    if stats.n == 0:
        ws.cell(row=2, column=1, value=_NO_DATA_TEXT)
        return
    ws.cell(row=2, column=1, value=stats.n)
    if stats.mean is not None:
        ws.cell(row=2, column=2, value=round(stats.mean, S.ROUND_PRECISION))
    if stats.sd is not None:
        ws.cell(row=2, column=3, value=round(stats.sd, S.ROUND_PRECISION))
    if stats.cv_pct is not None:
        ws.cell(row=2, column=4, value=round(stats.cv_pct, S.ROUND_PRECISION))


def _write_sheet3_去失控累计(
    wb: Workbook,
    conn: sqlite3.Connection,
    *,
    label: str,
    test_item_id: int,
    lot: str,
    level: str,
) -> None:
    """G.1 sheet 3: 去失控累计（第一条质控数据 ~ 上一自然月最后一天，排除失控 + 作废）。"""
    sheet_name = _truncate_sheet_name(_SHEET3_NAME.format(label=label))
    ws = wb.create_sheet(sheet_name)
    headers = ["N", "均值", "SD", "CV%", "范围说明"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=h)
    _fill_header_row(ws, headers)
    # 复用 §C 累计统计（默认 exclude_ooc=True：刨失控、保留警告、排除作废）
    stats = S.compute_cumulative_stats(
        conn, test_item_id=test_item_id, lot=lot, level=level,
    )
    interval = _describe_cumulative_range(conn, test_item_id, lot, level)
    ws.cell(row=2, column=5,
            value=f"{interval}，已排除'失控'与'作废'")
    if stats.n == 0:
        ws.cell(row=2, column=1, value=_NO_DATA_TEXT)
        return
    ws.cell(row=2, column=1, value=stats.n)
    if stats.mean is not None:
        ws.cell(row=2, column=2, value=round(stats.mean, S.ROUND_PRECISION))
    if stats.sd is not None:
        ws.cell(row=2, column=3, value=round(stats.sd, S.ROUND_PRECISION))
    if stats.cv_pct is not None:
        ws.cell(row=2, column=4, value=round(stats.cv_pct, S.ROUND_PRECISION))


def _describe_cumulative_range(
    conn: sqlite3.Connection,
    test_item_id: int,
    lot: str,
    level: str,
) -> str:
    """累计区间说明（2026-10-13 用户裁定：起点 = 第一条质控数据日期）。"""
    today = date.today()
    last_prev = S._last_day_of_prev_month(today)
    start_s = S.first_data_date(conn, test_item_id, lot, level)
    if start_s is None:
        return "无质控数据"
    if start_s > last_prev.isoformat():
        return (f"第一条质控数据 {start_s} 晚于上一自然月最后一天 "
                f"{last_prev}，无累计数据")
    return f"第一条质控数据 {start_s} ~ 上一自然月最后一天 {last_prev}（不含当月）"


def _write_sheet4_质控图(
    wb: Workbook,
    conn: sqlite3.Connection,
    *,
    label: str,
    project_name: str,
    test_item_name: str,
    level: str,
    lot: str,
    year: int,
    month: int,
) -> None:
    """G.1 sheet 4: 质控图（用户需求 2026-10-12 第二次补充）。

    - 同指标名同批号存在 L+H 两水平 → 嵌入【一图双 y 轴】
      （render_lj_chart_dual_axis，左轴 L、右轴 H，控制限重合；
      spec §10 第 14 条经用户拍板开放）。
    - 只有单水平 → 嵌入单水平图（render_lj_chart）。
    - 该月无数据 → 嵌入占位图（各渲染函数内置 "No Data" 处理）。
    """
    # 延迟导入：openpyxl 嵌图依赖 Pillow；lj_chart 依赖 matplotlib
    from openpyxl.drawing.image import Image as XlsxImage
    from . import lj_chart as LJ

    sheet_name = _truncate_sheet_name(_SHEET4_NAME.format(label=label))
    ws = wb.create_sheet(sheet_name)
    ws.cell(row=1, column=1,
            value=f"{project_name} · {test_item_name} · 批号 {lot} · "
                  f"{year:04d}-{month:02d} 质控图（双 y 轴：左 L，右 H）")

    pid = _resolve_project(conn, project_name)
    meta = LJ.make_default_meta()

    sibling = conn.execute(
        "SELECT level FROM test_items "
        "WHERE project_id = ? AND name = ? AND lot = ? AND level != ? LIMIT 1",
        (pid, test_item_name, lot, level),
    ).fetchone()

    if sibling is not None:
        # 双水平：直接查 L / H 两水平（sibling 存在保证两水平都在）
        data_l = LJ.compute_chart_data(
            conn, project_id=pid, test_item_name=test_item_name,
            lot=lot, level="L", year=year, month=month,
        )
        data_h = LJ.compute_chart_data(
            conn, project_id=pid, test_item_name=test_item_name,
            lot=lot, level="H", year=year, month=month,
        )
        fig = LJ.render_lj_chart_dual_axis(data_l, data_h, meta=meta)
        disp_w, disp_h = 880, 520   # 11:6.5 比例
    else:
        data = LJ.compute_chart_data(
            conn, project_id=pid, test_item_name=test_item_name,
            lot=lot, level=level, year=year, month=month,
        )
        fig = LJ.render_lj_chart(data, meta=meta)
        disp_w, disp_h = 880, 520    # 11:6.5 比例

    png_bytes = LJ.save_png_to_bytes(fig, dpi=150)
    img = XlsxImage(io.BytesIO(png_bytes))
    img.width, img.height = disp_w, disp_h
    ws.add_image(img, "A3")


def build_monthly_report_xlsx(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    year: int,
    month: int,
    selected_test_item_ids: list[int],
) -> bytes:
    """G.1 主入口：生成多 sheet xlsx 月报。

    流程：
        1. 校验项目存在 + 至少勾选 1 个指标
        2. 选中的每个 test_item → 3 个 sheet：本月原始 / 累计统计 / 去失控累计
    """
    if not selected_test_item_ids:
        raise NoIndicatorSelected()
    _resolve_project(conn, project_name)
    wb = Workbook()
    # 删默认 sheet
    default = wb.active
    if default is not None:
        wb.remove(default)
    for ti_id in selected_test_item_ids:
        ti_row = conn.execute(
            "SELECT name, level, lot FROM test_items WHERE id = ?", (ti_id,)
        ).fetchone()
        if ti_row is None:
            # 跳过已被删的指标
            continue
        label = f"{ti_row['name']}-{ti_row['level']}"
        _write_sheet1_本月原始(
            wb, conn,
            label=label, project_name=project_name,
            test_item_name=ti_row["name"], level=ti_row["level"],
            lot=ti_row["lot"],
            test_item_id=ti_id, year=year, month=month,
        )
        _write_sheet2_累计统计(
            wb, conn,
            label=label, test_item_id=ti_id,
            lot=ti_row["lot"], level=ti_row["level"],
        )
        _write_sheet3_去失控累计(
            wb, conn,
            label=label, test_item_id=ti_id,
            lot=ti_row["lot"], level=ti_row["level"],
        )
        _write_sheet4_质控图(
            wb, conn,
            label=label, project_name=project_name,
            test_item_name=ti_row["name"], level=ti_row["level"],
            lot=ti_row["lot"],
            year=year, month=month,
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================================
# G.2 失控记录导出
# ============================================================================

# out_of_control 表全字段（spec G.2 d + §8.1 + 用户决议"两个都导出"）
# "审核备注"列在表里没有独立字段，作为可选备注列保留为空
_OOC_COLUMNS: list[tuple[str, str]] = [
    ("触发日期",   "triggered_date"),
    ("检测指标",   "test_item_name"),
    ("项目",       "project_name"),
    ("水平",       "level"),
    ("批号",       "lot"),
    ("规则",       "rules"),
    ("处理过程",   "conclusion"),
    ("已检查项",   "checklist"),
    ("处理人",     "operator"),
    ("审核人",     "reviewed_by"),
    ("审核备注",   ""),       # 表中无独立字段，保留列以满足 spec G.2 d 列出项
    ("样本批次",   "sample_batch"),
    ("样本编号",   "sample_no"),
    ("误差类型",   "error_type"),
    ("创建时间",   "created_at"),
]


def has_ooc_in_range(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    date_from: str,
    date_to_inclusive: str,
) -> bool:
    """G.2 边界：选时间段无失控记录 → 提示'该时间段无失控记录'。"""
    pid = _resolve_project(conn, project_name)
    row = conn.execute(
        "SELECT 1 FROM out_of_control "
        "WHERE project_id = ? AND triggered_date >= ? AND triggered_date <= ? "
        "LIMIT 1",
        (pid, date_from, date_to_inclusive),
    ).fetchone()
    return row is not None


def _query_ooc_rows(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    date_from: str,
    date_to_inclusive: str,
) -> tuple[list[sqlite3.Row], list[tuple[str, str]]]:
    """返回 (rows, cols)；cols 排除表里没有对应字段的"审核备注"等。"""
    pid = _resolve_project(conn, project_name)
    real_cols = [(zh, key) for (zh, key) in _OOC_COLUMNS if key]
    cols = [c[1] for c in real_cols]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM out_of_control "
        f"WHERE project_id = ? "
        f"  AND triggered_date >= ? AND triggered_date <= ? "
        f"ORDER BY triggered_date, id",
        (pid, date_from, date_to_inclusive),
    ).fetchall()
    return rows, real_cols


def _render_ooc_cell(col_key: str, raw: object) -> object:
    """把 rules / checklist 这类 JSON 字段渲染为可读字符串。"""
    if raw is None:
        return ""
    if col_key in ("rules", "checklist"):
        try:
            parsed = json.loads(str(raw))
            if isinstance(parsed, list):
                return ", ".join(str(x) for x in parsed)
        except (TypeError, ValueError):
            return str(raw)
        return str(raw)
    return raw


def build_ooc_report_xlsx(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    date_from: str,
    date_to_inclusive: str,
) -> bytes:
    """G.2 xlsx：失控记录全字段导出。"""
    rows, real_cols = _query_ooc_rows(
        conn, project_name=project_name,
        date_from=date_from, date_to_inclusive=date_to_inclusive,
    )
    wb = Workbook()
    ws = wb.active
    ws.title = "失控记录"
    # Header：全列（含"审核备注"等空映射）
    headers = [c[0] for c in _OOC_COLUMNS]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=h)
    _fill_header_row(ws, headers)
    # Meta block
    ws.cell(row=1, column=len(headers) + 2, value="项目")
    ws.cell(row=1, column=len(headers) + 3, value=project_name)
    ws.cell(row=2, column=len(headers) + 2, value="区间")
    ws.cell(row=2, column=len(headers) + 3,
            value=f"{date_from} ~ {date_to_inclusive}")
    if not rows:
        ws.cell(row=2, column=1, value="该时间段无失控记录")
        ws.cell(row=2, column=1).font = Font(italic=True, color="C00000")
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    # 写数据：r 里的列顺序与 real_cols 一致，索引 i 与 _OOC_COLUMNS 中相同 i 处的 key 对应
    for r_idx, r in enumerate(rows, start=2):
        for ri, (_zh, key) in enumerate(real_cols):
            # 找 _OOC_COLUMNS 中 key 所在的列号
            col_pos = next(
                i for i, (_zh2, k2) in enumerate(_OOC_COLUMNS) if k2 == key
            ) + 1  # 1-based column
            ws.cell(row=r_idx, column=col_pos,
                    value=_render_ooc_cell(key, r[ri]))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# G.2 PDF 矢量导出（reportlab）
# ---------------------------------------------------------------------------

# spec G.2 b 验收：字号 ≥ 10pt。表头略大些，正文 ≥ 10。
_PDF_TITLE_PT = 14
_PDF_HEADER_PT = 11
_PDF_BODY_PT = 10
_PDF_LINE_GAP_PT = 4  # 每行额外间距


def build_ooc_report_pdf(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    date_from: str,
    date_to_inclusive: str,
) -> bytes:
    """G.2 pdf：失控记录全字段矢量 PDF（spec G.2 b）。"""
    # 延迟导入，避免 openpyxl-only 用户付出 reportlab 启动成本
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    # 注册中文字体（reportlab 内置 STSong-Light，无需本地字体文件）
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        cjk_font = "STSong-Light"
    except Exception:
        cjk_font = "Helvetica"

    rows, real_cols = _query_ooc_rows(
        conn, project_name=project_name,
        date_from=date_from, date_to_inclusive=date_to_inclusive,
    )

    buf = io.BytesIO()
    # 横向 A4，矢量绘制；关闭压缩以确保 PDF 流里能搜到 Tf 字号操作符
    page_size = landscape(A4)
    c = rl_canvas.Canvas(buf, pagesize=page_size, pageCompression=0)
    page_w, page_h = page_size
    margin = 15 * mm

    def _draw_header() -> None:
        c.setFont(cjk_font, _PDF_TITLE_PT)
        c.drawString(margin, page_h - margin, f"失控记录报表 —— {project_name}")
        c.setFont(cjk_font, _PDF_BODY_PT)
        c.drawString(
            margin, page_h - margin - 6 * mm,
            f"区间：{date_from} ~ {date_to_inclusive}   "
            f"实验室：{_lab_name_or_blank(conn)}   "
            f"记录数：{len(rows)}",
        )
        c.setFont(cjk_font, _PDF_HEADER_PT)
        # 表头列位置：使用全列（_OOC_COLUMNS）以保证 PDF 列与 xlsx 列一致
        n = len(_OOC_COLUMNS)
        col_w = (page_w - 2 * margin) / n
        y = page_h - margin - 12 * mm
        for i, (zh, _key) in enumerate(_OOC_COLUMNS):
            c.drawString(margin + i * col_w, y, zh)
        c.line(margin, y - 2, page_w - margin, y - 2)
        return y - 8  # 起始 y

    def _draw_row(y: float, r: sqlite3.Row) -> float:
        c.setFont(cjk_font, _PDF_BODY_PT)
        n = len(_OOC_COLUMNS)
        col_w = (page_w - 2 * margin) / n
        # real_cols 与 _OOC_COLUMNS 子集顺序一致；按全列位置填值
        for ri, (_zh, key) in enumerate(real_cols):
            # 找 _OOC_COLUMNS 中 key 所在的列号
            col_pos = next(
                i for i, (_zh2, k2) in enumerate(_OOC_COLUMNS) if k2 == key
            )
            val = r[ri]
            text = _render_ooc_cell(key, val)
            text_str = str(text) if text is not None else ""
            # 简单截断避免溢出
            if len(text_str) > 30:
                text_str = text_str[:27] + "..."
            c.drawString(margin + col_pos * col_w, y, text_str)
        return y - 6 * mm

    if not rows:
        y = _draw_header()
        c.setFont(cjk_font, _PDF_BODY_PT)
        c.setFillColorRGB(0.7, 0.0, 0.0)
        c.drawString(margin, y - 4 * mm, "该时间段无失控记录")
        c.setFillColorRGB(0, 0, 0)
    else:
        y = _draw_header()
        for r in rows:
            if y < margin + 12 * mm:
                c.showPage()
                y = _draw_header()
            y = _draw_row(y, r)
    c.save()
    return buf.getvalue()


def _lab_name_or_blank(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT lab_name FROM lab_settings WHERE id = 1"
    ).fetchone()
    if row is None or not row["lab_name"]:
        return ""
    return str(row["lab_name"])


__all__ = [
    # data
    "TestItemIndicator",
    # exceptions
    "NoIndicatorSelected", "ProjectNotFound",
    # G.1
    "select_test_items_for_month",
    "monthly_has_data",
    "build_monthly_report_xlsx",
    # G.2
    "has_ooc_in_range",
    "build_ooc_report_xlsx",
    "build_ooc_report_pdf",
]
