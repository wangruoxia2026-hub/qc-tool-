"""
Functional E: Levey-Jennings QC Chart (spec V1.3 §E.1-E.2 + §10 boundary)

Public API
----------
- LJChartPoint         : one plotted point (date, value, sd_score, status enum, rules)
- LJChartData          : everything the renderer needs
- ChartMeta            : rendering meta (6 visible fields + always-on date_range)
- make_default_meta    : build meta with all fields visible
- compute_chart_data   : business layer -> LJChartData (no matplotlib)
- render_lj_chart      : matplotlib -> Figure (7 lines + 3 point types + legend)
- render_lj_chart_dual_axis : matplotlib -> Figure (L+H one chart, dual y axes,
                              6 point styles, coincident control lines)
- build_tooltip_text   : (date, value, sd-score, [rules]) payload for hover

Design (spec §E.1-E.2 + AGENTS.md iron rule 1 + §10 15-item scope):
1. Read only from measurements.status / measurements.rules — no judgment changes.
2. Void rows never enter the working set.
3. Month scope = full calendar month of (year, month).
4. SD=0 -> only center line + "SD = 0, control limits invalid" notice.
5. Same day multiple seqs -> all kept; chart may x-offset them but no aggregation.
6. L+H dual-axis chart is the 2026-10-12 second user supplement (opens spec
   §10 item 14 by explicit user decision); single-level styles stay spec §E.1.
7. Only matplotlib; no PyQt6 in this file (UI layer uses these APIs only).
"""
from __future__ import annotations

import json
import io
import sqlite3
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

import matplotlib

matplotlib.use("Agg")  # safe default; UI layer may override

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

# 标题改为中文（2026-10-13 用户需求）后，matplotlib 默认字体不含 CJK 字形，
# 桌面图/报表 PNG 需显式指定中文字体；找不到时回退默认字体（不阻断渲染）。
try:
    import matplotlib.font_manager as _fm
    _installed = {f.name for f in _fm.fontManager.ttflist}
    _cjk_font = next(
        (f for f in ("Microsoft YaHei", "SimHei", "SimSun") if f in _installed),
        None,
    )
    if _cjk_font:
        matplotlib.rcParams["font.sans-serif"] = [_cjk_font, "DejaVu Sans"]
except Exception:
    pass  # 字体扫描失败不阻断图表渲染
matplotlib.rcParams["axes.unicode_minus"] = False

# Status mapping is local to this module (see _ChartStatus below).
class _ChartStatus(str, Enum):
    IN_CONTROL = "in_control"
    WARNING = "warning"
    OUT_OF_CONTROL = "out_of_control"
    VOID = "void"

ROUND_PRECISION = 3  # spec §D.5


# ============================================================================
# Data classes
# ============================================================================

@dataclass
class LJChartPoint:
    """One plotted point on the Levey-Jennings chart."""
    date: str           # 'YYYY-MM-DD'
    seq: int            # sequence number within date (tie-breaker for same-day offset)
    value: float
    sd_score: float     # (value - target) / sd, rounded to 3dp; NaN when sd=0
    status: _ChartStatus  # IN_CONTROL / WARNING / OUT_OF_CONTROL / VOID
    rules: list[str]    # JSON-parsed list of triggered rule codes; [] for in-control
    level: str
    lot: str
    measurement_id: int


@dataclass
class LJChartData:
    """Everything the renderer needs for one (project, item, lot, level, month)."""
    points: list[LJChartPoint]
    target: float
    sd: float
    project_name: str
    project_short: Optional[str]
    test_item_name: str
    test_item_short: Optional[str]
    unit: Optional[str]
    manufacturer: Optional[str]
    lot: str
    level: str
    operator: Optional[str]
    pre_processor: Optional[str]
    year: int
    month: int
    date_range: tuple[str, str]    # ('YYYY-MM-DD', 'YYYY-MM-DD')


# 12 user-facing chart fields (spec §E.2 f). 标题新格式（用户拍板 2026-10-13）
# 只渲染其中 6 个（见 TITLE_FIELDS）；其余 5 个不再上标题，但保留在
# ALL_FIELDS 里兼容 Web info 参数历史值。
ALL_FIELDS: tuple[str, ...] = (
    "project_name",
    "test_item_name",
    "test_item_short",
    "unit",
    "manufacturer",
    "lot",
    "level",
    "target",
    "sd",
    "operator",
    "pre_processor",
    "date_range",
)

# 新标题格式实际渲染的 6 个可配置字段（date_range 由 make_default_meta 恒显示）。
TITLE_FIELDS: tuple[str, ...] = (
    "project_name",
    "lot",
    "target",
    "sd",
    "operator",
    "pre_processor",
)


@dataclass
class ChartMeta:
    """Rendering meta: title text composition + visible fields.

    新标题格式只渲染 TITLE_FIELDS 6 项（+恒显示的 date_range）；
    visible_fields 里出现的历史键（如 unit/level）被忽略。
    """
    visible_fields: set[str] = field(default_factory=lambda: set(ALL_FIELDS))
    # Optional title suffix (e.g. "SD = 0, control limits invalid")
    notice: Optional[str] = None


def make_default_meta(visible_fields: Optional[Iterable[str]] = None) -> ChartMeta:
    """Build ChartMeta with all title fields visible by default.

    新标题格式（2026-10-13）默认全显 TITLE_FIELDS 6 项；`date_range` 恒显示
    （spec §E.2 f 兜底：用户须知道在看哪个月），即使全关也保留。
    """
    if visible_fields is None:
        return ChartMeta(visible_fields=set(TITLE_FIELDS) | {"date_range"})
    fields = set(visible_fields)
    fields.add("date_range")  # always-on fallback
    return ChartMeta(visible_fields=fields)


# ============================================================================
# Status text -> enum mapping (spec §B.1)
# ============================================================================

def _parse_status(status_text: Optional[str]) -> Optional[_ChartStatus]:
    """Map spec §B.1 status text to _ChartStatus.

    spec §B.1 状态列字串为中文：'在控' / '警告(规则)' / '失控(规则)' / '作废'
    （见 measurements._status_text）；同时兼容早期英文 token
    （'InControl' / 'Warning(...)' / 'OutOfControl(...)' / 'Void'）。
    'Void' / '作废' / None -> VOID（图表数据层会排除该行，图上不显示，spec §E.1）。
    Unknown labels fall through to IN_CONTROL (conservative default; not an
    error path).
    """
    if status_text is None:
        return _ChartStatus.VOID
    s = status_text.strip()
    if s in ("Void", "void", "作废"):
        return _ChartStatus.VOID
    if s.startswith("OutOfControl") or s.startswith("失控"):
        return _ChartStatus.OUT_OF_CONTROL
    if s.startswith("Warning") or s.startswith("警告"):
        return _ChartStatus.WARNING
    if s in ("InControl", "在控"):
        return _ChartStatus.IN_CONTROL
    # Unknown / future labels: treat as in-control (not an error path)
    return _ChartStatus.IN_CONTROL


# ============================================================================
# Business layer: compute_chart_data
# ============================================================================

def compute_chart_data(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    test_item_name: str,
    lot: str,
    level: str,
    year: int,
    month: int,
) -> LJChartData:
    """Build chart data for one (project, test_item, lot, level, month).

    Reads only from test_items + measurements tables; no judgment logic,
    no matplotlib dependency at this layer (it is pure-data, fast).
    """
    # 1. Resolve test_item row (project_id + name + lot + level).
    #    If not found -> empty points, sensible defaults.
    ti_row = conn.execute(
        "SELECT t.*, p.name AS project_name, p.short AS project_short "
        "FROM test_items t JOIN projects p ON p.id = t.project_id "
        "WHERE t.project_id = ? AND t.name = ? AND t.lot = ? AND t.level = ?",
        (project_id, test_item_name, lot, level),
    ).fetchone()

    target: float = 0.0
    sd: float = 0.0
    project_name: str = ""
    project_short: Optional[str] = None
    test_item_short: Optional[str] = None
    unit: Optional[str] = None
    manufacturer: Optional[str] = None
    operator_v: Optional[str] = None
    pre_processor_v: Optional[str] = None

    if ti_row is not None:
        target = float(ti_row["target"])
        sd = float(ti_row["sd"])
        project_name = ti_row["project_name"]
        project_short = ti_row["project_short"]
        test_item_short = ti_row["short"]
        unit = ti_row["unit"]
        manufacturer = ti_row["manufacturer"]
        operator_v = ti_row["operator"]
        pre_processor_v = ti_row["pre_processor"]
    else:
        # Best-effort project name for an empty result
        p_row = conn.execute(
            "SELECT name, short FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if p_row is not None:
            project_name = p_row["name"]
            project_short = p_row["short"]

    # 2. Compute month date range
    last_day = monthrange(year, month)[1]
    date_from = f"{year:04d}-{month:02d}-01"
    date_to = f"{year:04d}-{month:02d}-{last_day:02d}"

    # 3. Fetch measurements within month.
    #    spec §E.1（2026-10-12 用户确认）：作废点不显示 —— 数据层直接排除
    #    作废行。作废行在判定/统计层的排除由各自模块负责，与本模块无关。
    rows = conn.execute(
        "SELECT id, date, seq, value, status, rules "
        "FROM measurements "
        "WHERE project_id = ? AND lot = ? AND level = ? "
        "  AND date >= ? AND date <= ? "
        "  AND test_item_id IN ("
        "    SELECT id FROM test_items "
        "    WHERE project_id = ? AND name = ? AND lot = ? AND level = ?"
        "  ) "
        "ORDER BY date, seq, id",
        (project_id, lot, level, date_from, date_to,
         project_id, test_item_name, lot, level),
    ).fetchall()

    points: list[LJChartPoint] = []
    for r in rows:
        # Map status text -> enum
        st = _parse_status(r["status"])
        if st == _ChartStatus.VOID:
            continue  # 作废点不显示（spec §E.1）
        v = float(r["value"])
        if sd > 0:
            sd_score = round((v - target) / sd, ROUND_PRECISION)
        else:
            sd_score = float("nan")
        # rules JSON
        rules_raw = r["rules"]
        rules: list[str] = []
        if rules_raw:
            try:
                parsed = json.loads(rules_raw)
                if isinstance(parsed, list):
                    rules = [str(x) for x in parsed]
            except (TypeError, ValueError):
                rules = []
        points.append(LJChartPoint(
            date=r["date"],
            seq=int(r["seq"]),
            value=v,
            sd_score=sd_score,
            status=st,
            rules=rules,
            level=level,
            lot=lot,
            measurement_id=int(r["id"]),
        ))

    return LJChartData(
        points=points,
        target=target,
        sd=sd,
        project_name=project_name,
        project_short=project_short,
        test_item_name=test_item_name,
        test_item_short=test_item_short,
        unit=unit,
        manufacturer=manufacturer,
        lot=lot,
        level=level,
        operator=operator_v,
        pre_processor=pre_processor_v,
        year=year,
        month=month,
        date_range=(date_from, date_to),
    )


# ============================================================================
# Tooltip text
# ============================================================================

def build_tooltip_text(p: LJChartPoint) -> str:
    """Build hover tooltip payload: (date, value, sd-deviation, [rules]).

    Per spec §E.1 c: show '(date, value, sd-deviation)'; if 1_2s or out-of-control,
    append triggered rule list.
    """
    sd_text = (
        f"{p.sd_score:+.3f}" if p.sd_score == p.sd_score  # not NaN
        else "NaN"
    )
    parts = [
        f"Date: {p.date}",
        f"Value: {p.value:.3f}",
        f"SD: {sd_text}",
    ]
    # Append rule list when triggered
    if p.status in (_ChartStatus.WARNING, _ChartStatus.OUT_OF_CONTROL):
        if p.rules:
            parts.append("Rules: " + ", ".join(p.rules))
        else:
            parts.append("Rules: (none)")
    elif p.status == _ChartStatus.VOID:
        parts.append("Status: Void (excluded from stats)")
    return "\n".join(parts)


# ============================================================================
# Rendering
# ============================================================================

# Spec §E.1 e color & line-style spec
_COLOR_CENTER = "#1f7a1f"   # green
_COLOR_1SD = "#1f7a1f"
_COLOR_2SD = "#d9a400"      # yellow
_COLOR_3SD = "#c0392b"      # red
_COLOR_IN_CTRL = "black"
_COLOR_WARNING = "#d9a400"
_COLOR_OOC = "#c0392b"

# 双轴图专用点样式（用户需求 2026-10-12 第二次补充，取代并排双图）：
# L 在控绿实心圆 / 警告黄实心圆 / 失控红实心圆；
# H 在控蓝实心三角 / 警告黄实心三角 / 失控红实心三角。
# 单水平图样式保持 spec §E.1（在控黑圆点 / 警告黄三角 / 失控红圆）。
_COLOR_IN_CTRL_L = "#1f7a1f"   # green filled circle (L in-control)
_COLOR_IN_CTRL_H = "#1f5fa3"   # blue filled triangle (H in-control)

# 双轴图可见 SD 跨度封顶（用户需求 2026-10-13）：
# 数据出现极端离群值时 y 轴最多撑到 ±4SD，控制限不被压缩；
# 超出 ±4SD 的点钉在上下边缘并标注实际值（与 Web 单图行为一致）。
_DUAL_SPAN_CAP_SD = 4.0

_DUAL_POINT_STYLE = {
    "L": {
        _ChartStatus.IN_CONTROL: ("o", _COLOR_IN_CTRL_L),
        _ChartStatus.WARNING: ("o", _COLOR_WARNING),
        _ChartStatus.OUT_OF_CONTROL: ("o", _COLOR_OOC),
    },
    "H": {
        _ChartStatus.IN_CONTROL: ("^", _COLOR_IN_CTRL_H),
        _ChartStatus.WARNING: ("^", _COLOR_WARNING),
        _ChartStatus.OUT_OF_CONTROL: ("^", _COLOR_OOC),
    },
}

_NO_DATA_MSG = "No Data"


def _build_title_lines(data: LJChartData, meta: ChartMeta) -> list[str]:
    """Compose chart title lines honoring meta.visible_fields.

    新标题格式（用户需求 2026-10-13，取代原英文 key=value 格式）：
        项目名称+质控图（首行，渲染层字号 ×2）
        质控品批号：…   靶值（L）=…   SD（L）=…（批号+靶值/SD 一行平铺）
        仪器操作： / 前处理： / 时间范围：
    只渲染 TITLE_FIELDS 6 项；可选字段为空时跳过该行；
    date_range 恒显示（make_default_meta 兜底，spec §E.2 f）。
    """
    v = meta.visible_fields
    lines: list[str] = []

    if "project_name" in v and data.project_name:
        lines.append(f"{data.project_name}质控图")

    # 批号 + 靶值/SD 合并一行平铺（用户需求 2026-10-13 第二次调整）
    row_bits: list[str] = []
    if "lot" in v and data.lot:
        row_bits.append(f"质控品批号：{data.lot}")
    tgt_sd_bits: list[str] = []
    if "target" in v:
        tgt_sd_bits.append(f"靶值（{data.level}）={data.target:.{ROUND_PRECISION}f}")
    if "sd" in v:
        tgt_sd_bits.append(f"SD（{data.level}）={data.sd:.{ROUND_PRECISION}f}")
    if tgt_sd_bits:
        row_bits.append("   ".join(tgt_sd_bits))
    if row_bits:
        lines.append("   ".join(row_bits))

    if "operator" in v and data.operator:
        lines.append(f"仪器操作：{data.operator}")
    if "pre_processor" in v and data.pre_processor:
        lines.append(f"前处理：{data.pre_processor}")

    if "date_range" in v and data.date_range:
        lines.append(f"时间范围：{data.date_range[0]}~{data.date_range[1]}")

    if meta.notice:
        lines.append(meta.notice)

    return lines


def _set_styled_title(fig: Figure, ax, title_lines: list[str],
                      fontsize: int) -> None:
    """首行「项目名质控图」字号 ×2（用户需求 2026-10-13 第二次调整）。

    首行放 fig.suptitle（大字号），其余行放 ax.set_title（常规字号）；
    只有一行时整体用大字号放 ax。
    """
    if not title_lines:
        return
    if len(title_lines) == 1:
        ax.set_title(title_lines[0], fontsize=fontsize * 2)
        return
    fig.suptitle(title_lines[0], fontsize=fontsize * 2)
    ax.set_title("\n".join(title_lines[1:]), fontsize=fontsize)


def _empty_figure(title_lines: list[str], message: str) -> Figure:
    fig = Figure(figsize=(11, 6))
    ax = fig.add_subplot(111)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    _set_styled_title(fig, ax, title_lines, 11)
    ax.text(0.5, 0.5, message, ha="center", va="center",
            transform=ax.transAxes, fontsize=18, color="#888888")
    return fig


def _render_single_axes(fig: Figure, ax, data: LJChartData, *,
                        meta: ChartMeta) -> None:
    """Draw ONE level's LJ chart onto an existing Axes.

    Shared core used by:
    - render_lj_chart        (single level, whole figure)

    Handles all three cases on the given ax:
    1. no points  -> blank axes + centered "No Data" placeholder
    2. sd = 0     -> center line only + notice in title (spec §9)
    3. normal     -> 7 control lines + 4-status points + legend

    Title: first line (project name) on fig.suptitle at 2x size,
    remaining lines on ax.set_title (see _set_styled_title).
    Callers own fig.tight_layout().
    """
    title_lines = _build_title_lines(data, meta)

    # Case 1: no points -> placeholder on this ax
    if not data.points:
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        _set_styled_title(fig, ax, title_lines, 9)
        ax.text(0.5, 0.5, _NO_DATA_MSG, ha="center", va="center",
                transform=ax.transAxes, fontsize=16, color="#888888")
        return

    target = data.target
    sd = data.sd
    sd_zero = (sd == 0)

    # X axis: enumerate points by (date, seq) preserving order
    x_labels: list[str] = []
    xs: list[int] = []
    ys: list[float] = []
    for i, p in enumerate(data.points, start=1):
        x_labels.append(p.date if p.seq == 0 else f"{p.date}#{p.seq}")
        xs.append(i)
        ys.append(p.value)

    # 7 control lines (or only center when sd=0)
    line_specs = []
    if sd_zero:
        line_specs.append((0, _COLOR_CENTER, "-", "Center"))
    else:
        for k, color, ls, lbl in [
            (-3, _COLOR_3SD, "-",  "-3SD"),
            (-2, _COLOR_2SD, "--", "-2SD"),
            (-1, _COLOR_1SD, "--", "-1SD"),
            ( 0, _COLOR_CENTER, "-", "Center"),
            ( 1, _COLOR_1SD, "--", "+1SD"),
            ( 2, _COLOR_2SD, "--", "+2SD"),
            ( 3, _COLOR_3SD, "-",  "+3SD"),
        ]:
            line_specs.append((k, color, ls, lbl))

    for k, color, ls, lbl in line_specs:
        y_val = target + k * sd if not sd_zero else target
        ax.axhline(
            y_val, color=color, linestyle=ls, linewidth=1.2, alpha=0.85,
            label=lbl,
        )

    # Case 2: SD=0 -> notice + only center line is drawn.
    if sd_zero:
        meta_local = ChartMeta(
            visible_fields=meta.visible_fields,
            notice=(meta.notice or "SD = 0, control limits invalid"),
        )
        title_lines = _build_title_lines(data, meta_local)
        _set_styled_title(fig, ax, title_lines, 9)

        # Still plot the points so user sees they exist.
        for x, y in zip(xs, ys):
            ax.plot(x, y, marker="o", color=_COLOR_IN_CTRL, markersize=6)
        ax.set_xlabel("point index")
        ax.set_ylabel("value")
        ax.set_xticks(xs)
        ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)
        ax.grid(True, alpha=0.25)
        legend_handles = [
            Line2D([0], [0], color=_COLOR_CENTER, lw=1.5, label="Center"),
            Line2D([0], [0], marker="o", color="w",
                   markerfacecolor=_COLOR_IN_CTRL, markersize=7, label="InControl"),
        ]
        ax.legend(handles=legend_handles, loc="upper right", fontsize=9)
        return

    # Case 3: normal — plot points by status — 3 styles (spec §E.1):
    # in-control (black o), warning (yellow ^), out-of-control (red o).
    # 作废行已在数据层排除，不绘制。
    style_by_status = {
        _ChartStatus.IN_CONTROL:    ("o", _COLOR_IN_CTRL,  "InControl"),
        _ChartStatus.WARNING:       ("^", _COLOR_WARNING,  "Warning"),
        _ChartStatus.OUT_OF_CONTROL: ("o", _COLOR_OOC,      "OutOfControl"),
    }
    for x, y, p in zip(xs, ys, data.points):
        marker, color, _ = style_by_status.get(
            p.status, ("o", _COLOR_IN_CTRL, "InControl")
        )
        ax.plot(x, y, marker=marker, color=color, markersize=8,
                markeredgecolor="black" if marker != "x" else color,
                markeredgewidth=0.4)

    # Axes / labels
    ax.set_xlabel("Date")
    ax.set_ylabel("Measured value")
    _set_styled_title(fig, ax, title_lines, 9)
    ax.set_xticks(xs)
    ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)

    # Y range: include ±3SD with a margin
    y_lo = min(ys) - 0.5 * sd
    y_hi = max(ys) + 0.5 * sd
    y_lo = min(y_lo, target - 3 * sd)
    y_hi = max(y_hi, target + 3 * sd)
    ax.set_ylim(y_lo, y_hi)

    ax.grid(True, alpha=0.25)

    # Custom legend (lines + 3 point types)
    legend_handles = [
        Line2D([0], [0], color=_COLOR_CENTER, lw=1.5, label="Center"),
        Line2D([0], [0], color=_COLOR_1SD, linestyle="--", lw=1.2, label="+1SD"),
        Line2D([0], [0], color=_COLOR_2SD, linestyle="--", lw=1.2, label="+2SD"),
        Line2D([0], [0], color=_COLOR_3SD, linestyle="-", lw=1.2, label="+3SD"),
        Line2D([0], [0], marker="o", color="w",
               markerfacecolor=_COLOR_IN_CTRL, markersize=8, label="InControl"),
        Line2D([0], [0], marker="^", color="w",
               markerfacecolor=_COLOR_WARNING, markersize=9, label="Warning"),
        Line2D([0], [0], marker="o", color="w",
               markerfacecolor=_COLOR_OOC, markersize=8, label="OutOfControl"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=9)


def render_lj_chart(
    data: LJChartData,
    *,
    meta: ChartMeta,
) -> Figure:
    """Render the Levey-Jennings chart (single level).

    Returns a matplotlib.figure.Figure. Caller decides how to display / save it.
    """
    title_lines = _build_title_lines(data, meta)

    # Edge: no data -> placeholder figure
    if not data.points:
        return _empty_figure(title_lines, _NO_DATA_MSG)

    fig = Figure(figsize=(11, 6.5))
    ax = fig.add_subplot(111)
    _render_single_axes(fig, ax, data, meta=meta)
    fig.tight_layout()
    return fig


def _dual_span_sd(data: LJChartData) -> Optional[float]:
    """一侧的最大 |SD 偏离| + 0.5 边距（下限 3.5）；无可用刻度时返回 None。"""
    if data.sd <= 0 or not data.points:
        return None
    k = max(abs(p.sd_score) for p in data.points if p.sd_score == p.sd_score)
    return max(3.5, k + 0.5)


def _dual_title_lines(data_l: LJChartData, data_h: LJChartData,
                      meta: ChartMeta) -> list[str]:
    """双轴图标题（新格式，用户需求 2026-10-13 + 一行平铺第二次调整）。

    首行=项目名+质控图（渲染层字号 ×2）；
    批号 + 靶值（L）/SD（L）+ 靶值（H）/SD（H）合并一行平铺；
    仪器操作/前处理 L/H 相同→一行，不同→（L）（H）两行；时间范围独立一行。
    """
    v = meta.visible_fields
    lines: list[str] = []

    if "project_name" in v and data_l.project_name:
        lines.append(f"{data_l.project_name}质控图")

    def _paired(label: str, lv: Optional[str], hv: Optional[str],
                checked: bool) -> list[str]:
        """相同→一行；不同→（L）（H）两行；一侧为空→只显示非空侧。"""
        if not checked:
            return []
        if lv and hv:
            if lv == hv:
                return [f"{label}：{lv}"]
            return [f"{label}（L）：{lv}", f"{label}（H）：{hv}"]
        if lv:
            return [f"{label}（L）：{lv}"]
        if hv:
            return [f"{label}（H）：{hv}"]
        return []

    # 批号 + 左右两水平靶值/SD 合并一行平铺（2026-10-13 第二次调整）
    row_bits: list[str] = []
    if "lot" in v:
        if data_l.lot and data_h.lot:
            if data_l.lot == data_h.lot:
                row_bits.append(f"质控品批号：{data_l.lot}")
            else:
                row_bits.append(f"质控品批号（L）：{data_l.lot}    "
                                f"质控品批号（H）：{data_h.lot}")
        elif data_l.lot:
            row_bits.append(f"质控品批号（L）：{data_l.lot}")
        elif data_h.lot:
            row_bits.append(f"质控品批号（H）：{data_h.lot}")
    for side, d in (("L", data_l), ("H", data_h)):
        bits: list[str] = []
        if "target" in v:
            bits.append(f"靶值（{side}）={d.target:.{ROUND_PRECISION}f}")
        if "sd" in v:
            bits.append(f"SD（{side}）={d.sd:.{ROUND_PRECISION}f}")
        if bits:
            row_bits.append("   ".join(bits))
    if row_bits:
        lines.append("   ".join(row_bits))

    lines += _paired("仪器操作", data_l.operator, data_h.operator,
                     "operator" in v)
    lines += _paired("前处理", data_l.pre_processor, data_h.pre_processor,
                     "pre_processor" in v)

    if "date_range" in v and data_l.date_range:
        lines.append(f"时间范围：{data_l.date_range[0]}~{data_l.date_range[1]}")

    if meta.notice:
        lines.append(meta.notice)

    return lines


def render_lj_chart_dual_axis(
    data_l: LJChartData,
    data_h: LJChartData,
    *,
    meta: ChartMeta,
) -> Figure:
    """Render L and H levels on ONE chart with dual y axes.

    用户需求（2026-10-12 第二次补充，取代并排双图；spec §10 第 14 条
    经用户拍板开放）：
    - 左 y 轴 = L 水平测定值，右 y 轴 = H 水平测定值，横轴 = 检测日期；
    - 两侧各 7 条控制限（靶值绿实线、±1SD 绿虚线、±2SD 黄虚线、
      ±3SD 红实线），左右必须重合；
    - 点样式：L 在控绿实心圆 / 警告黄实心圆 / 失控红实心圆；
      H 在控蓝实心三角 / 警告黄实心三角 / 失控红实心三角。

    重合实现：两轴 ylim 各取"本水平靶值 ± M×本水平 SD"，M = 两侧
    max(|SD 偏离|+0.5, 3.5) 取大，对称映射到同一像素范围 →
    同一 k·SD 线在左右两轴天然落到同一高度。

    - 一侧无点 -> 该侧只画控制限；
    - 一侧 SD=0 -> 该侧仅画中心线（与对侧中心线重合）+ 标题提示；
    - 两侧均无点 -> No Data 占位图；
    - 横轴同日 L/H 逐点配对同 x：该日 L、H 各自按（seq, measurement_id）
      排序后，第 i 个 L 点与第 i 个 H 点共用第 i 个横位（2026-10-13
      用户第四次调整）；日期刻度居该日组中点。
    """
    title_lines = _dual_title_lines(data_l, data_h, meta)

    # 两侧都无点 -> 占位图
    if not data_l.points and not data_h.points:
        return _empty_figure(title_lines, _NO_DATA_MSG)

    # SD=0 提示（按侧标注）
    sd_zero_sides = [d.level for d in (data_l, data_h) if d.sd == 0]
    if sd_zero_sides:
        title_lines = title_lines + [
            " / ".join(f"{lv} SD = 0, control limits invalid"
                       for lv in sd_zero_sides)
        ]

    fig = Figure(figsize=(11, 6.5))
    ax_l = fig.add_subplot(111)
    ax_h = ax_l.twinx()

    # ---- y 轴归一化（重合的关键）----
    m_l = _dual_span_sd(data_l)
    m_h = _dual_span_sd(data_h)
    # 可见跨度封顶 ±4SD：极端离群点不压缩控制限（超限点钉边 + 标注）
    M = min(max(m_l if m_l is not None else 3.5,
                m_h if m_h is not None else 3.5),
            _DUAL_SPAN_CAP_SD)
    ref_l = data_l.sd if data_l.sd > 0 else (data_h.sd if data_h.sd > 0 else 1.0)
    ref_h = data_h.sd if data_h.sd > 0 else (data_l.sd if data_l.sd > 0 else 1.0)
    ax_l.set_ylim(data_l.target - M * ref_l, data_l.target + M * ref_l)
    ax_h.set_ylim(data_h.target - M * ref_h, data_h.target + M * ref_h)

    # ---- 横轴：同日 L/H 逐点配对同 x（2026-10-13 用户第四次调整） ----
    # 每日期一组横位：该日 L、H 各自按（seq, measurement_id）排序后，
    # 第 i 个 L 点与第 i 个 H 点共用第 i 个横位；日期刻度居该组中点。
    lpts_by_date: dict[str, list[LJChartPoint]] = {}
    hpts_by_date: dict[str, list[LJChartPoint]] = {}
    for p in data_l.points:
        lpts_by_date.setdefault(p.date, []).append(p)
    for p in data_h.points:
        hpts_by_date.setdefault(p.date, []).append(p)
    for pts in list(lpts_by_date.values()) + list(hpts_by_date.values()):
        pts.sort(key=lambda p: (p.seq, p.measurement_id))
    dates = sorted(set(lpts_by_date) | set(hpts_by_date))
    x_by_id: dict[int, int] = {}
    date_center: dict[str, float] = {}
    pos = 1
    for d in dates:
        lps = lpts_by_date.get(d, [])
        hps = hpts_by_date.get(d, [])
        n = max(len(lps), len(hps))
        for i, p in enumerate(lps):
            x_by_id[p.measurement_id] = pos + i
        for i, p in enumerate(hps):
            x_by_id[p.measurement_id] = pos + i
        date_center[d] = pos + (n - 1) / 2
        pos += n

    # ---- 每侧在自己的轴上画控制限 + 点 ----
    for ax, d in ((ax_l, data_l), (ax_h, data_h)):
        if d.sd == 0:
            ax.axhline(d.target, color=_COLOR_CENTER, linestyle="-",
                       linewidth=1.2, alpha=0.85, label="Center")
        else:
            for k, color, ls, lbl in [
                (-3, _COLOR_3SD, "-", "-3SD"),
                (-2, _COLOR_2SD, "--", "-2SD"),
                (-1, _COLOR_1SD, "--", "-1SD"),
                (0, _COLOR_CENTER, "-", "Center"),
                (1, _COLOR_1SD, "--", "+1SD"),
                (2, _COLOR_2SD, "--", "+2SD"),
                (3, _COLOR_3SD, "-", "+3SD"),
            ]:
                ax.axhline(d.target + k * d.sd, color=color, linestyle=ls,
                           linewidth=1.2, alpha=0.85, label=lbl)
        styles = _DUAL_POINT_STYLE.get(d.level, _DUAL_POINT_STYLE["L"])
        default_style = styles[_ChartStatus.IN_CONTROL]
        for p in d.points:
            x = x_by_id[p.measurement_id]
            marker, color = styles.get(p.status, default_style)
            y = p.value
            pin_label: Optional[str] = None
            if d.sd > 0:
                # 超 ±4SD 的点钉在上下边缘 + 标注实际值（tooltip 仍为真实数据）
                y_top = d.target + _DUAL_SPAN_CAP_SD * d.sd
                y_bot = d.target - _DUAL_SPAN_CAP_SD * d.sd
                if p.value > y_top:
                    pin_label, y = f"↑{p.value:.3f}", y_top
                elif p.value < y_bot:
                    pin_label, y = f"↓{p.value:.3f}", y_bot
            ax.plot(x, y, marker=marker, color=color, markersize=8,
                    markeredgecolor="black", markeredgewidth=0.4)
            if pin_label is not None:
                ax.text(x, y, pin_label, fontsize=7, ha="center",
                        va="bottom" if "↑" in pin_label else "top",
                        color=color)

    # ---- 共享 X 轴 + 两侧 y 标签（日期刻度居各组中点） ----
    x_labels = dates
    xs = [date_center[d] for d in dates]
    ax_l.set_xticks(xs)
    ax_l.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)
    ax_l.set_xlabel("Date")
    ax_l.set_ylabel(f"Measured value ({data_l.level})")
    ax_h.set_ylabel(f"Measured value ({data_h.level})")
    _set_styled_title(fig, ax_l, title_lines, 9)
    ax_l.grid(True, alpha=0.25)

    # ---- 图例：控制限 4 条 + 六种点样式 ----
    legend_handles = [
        Line2D([0], [0], color=_COLOR_CENTER, lw=1.5, label="Center"),
        Line2D([0], [0], color=_COLOR_1SD, linestyle="--", lw=1.2, label="±1SD"),
        Line2D([0], [0], color=_COLOR_2SD, linestyle="--", lw=1.2, label="±2SD"),
        Line2D([0], [0], color=_COLOR_3SD, linestyle="-", lw=1.2, label="±3SD"),
        Line2D([0], [0], marker="o", color="w",
               markerfacecolor=_COLOR_IN_CTRL_L, markersize=8, label="L InControl"),
        Line2D([0], [0], marker="o", color="w",
               markerfacecolor=_COLOR_WARNING, markersize=8, label="L Warning"),
        Line2D([0], [0], marker="o", color="w",
               markerfacecolor=_COLOR_OOC, markersize=8, label="L OutOfControl"),
        Line2D([0], [0], marker="^", color="w",
               markerfacecolor=_COLOR_IN_CTRL_H, markersize=9, label="H InControl"),
        Line2D([0], [0], marker="^", color="w",
               markerfacecolor=_COLOR_WARNING, markersize=9, label="H Warning"),
        Line2D([0], [0], marker="^", color="w",
               markerfacecolor=_COLOR_OOC, markersize=9, label="H OutOfControl"),
    ]
    ax_l.legend(handles=legend_handles, loc="upper right",
                fontsize=7.5, ncol=2)
    fig.tight_layout()
    return fig


__all__ = [
    # data
    "LJChartPoint", "LJChartData", "ChartMeta",
    "ALL_FIELDS", "TITLE_FIELDS", "make_default_meta",
    # ops
    "compute_chart_data", "build_tooltip_text",
    # render
    "render_lj_chart", "render_lj_chart_dual_axis", "_render_single_axes",
    # G.3: PNG export (spec V1.3 §G.3)
    "save_png", "save_png_to_bytes",
]


# ============================================================================
# G.3 PNG 导出（spec V1.3 §G.3）
# ============================================================================

# 满足 spec G.3 + AGENTS.md §11：默认 DPI ≥ 300。
# spec 没有硬性规定数值，但 G.2 b 规定 PDF "DPI ≥ 300"、G.3 沿用同一质量档。
DEFAULT_PNG_DPI = 300


def save_png(
    fig: Figure,
    path: Union[str, Path],
    *,
    dpi: int = DEFAULT_PNG_DPI,
) -> Path:
    """把当前 Levey-Jennings 质控图保存为 PNG 文件。

    - 元信息是否显示由调用方在 render_lj_chart(meta=...) 时决定（spec G.3）。
    - dpi 默认 300（与 PDF 矢量质量档一致；用户可自定义更小 dpi 减小文件大小）。

    返回写入的 Path。
    """
    path = Path(path)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    return path


def save_png_to_bytes(
    fig: Figure,
    *,
    dpi: int = DEFAULT_PNG_DPI,
) -> bytes:
    """save_png 的内存版本：返回 PNG bytes（用于测试 / 流式返回）。"""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    return buf.getvalue()