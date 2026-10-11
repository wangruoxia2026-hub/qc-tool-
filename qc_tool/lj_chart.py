"""
Functional E: Levey-Jennings QC Chart (spec V1.3 §E.1-E.2 + §10 boundary)

Public API
----------
- LJChartPoint         : one plotted point (date, value, sd_score, status enum, rules)
- LJChartData          : everything the renderer needs
- ChartMeta            : rendering meta (target/sd context + 12 visible fields)
- make_default_meta    : build meta with all 12 fields visible
- compute_chart_data   : business layer -> LJChartData (no matplotlib)
- render_lj_chart      : matplotlib -> Figure (7 lines + 3 point types + legend)
- build_tooltip_text   : (date, value, sd-score, [rules]) payload for hover

Design (spec §E.1-E.2 + AGENTS.md iron rule 1 + §10 15-item scope):
1. Read only from measurements.status / measurements.rules — no judgment changes.
2. Void rows never enter the working set.
3. Month scope = full calendar month of (year, month).
4. SD=0 -> only center line + "SD = 0, control limits invalid" notice.
5. Same day multiple seqs -> all kept; chart may x-offset them but no aggregation.
6. No cross-level comparison chart (spec §10 item 7).
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


# 12 user-facing chart fields (spec §E.2 f).
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


@dataclass
class ChartMeta:
    """Rendering meta: title text composition + 12 visible fields."""
    visible_fields: set[str] = field(default_factory=lambda: set(ALL_FIELDS))
    # Optional title suffix (e.g. "SD = 0, control limits invalid")
    notice: Optional[str] = None


def make_default_meta(visible_fields: Optional[Iterable[str]] = None) -> ChartMeta:
    """Build ChartMeta with all 12 fields visible by default.

    `date_range` is always a fallback field (spec §E.2 f implicitly requires
    the user to know *which month* they're looking at); even when the user
    toggles everything off, date_range stays on.
    """
    if visible_fields is None:
        return ChartMeta(visible_fields=set(ALL_FIELDS))
    fields = set(visible_fields)
    fields.add("date_range")  # always-on fallback
    return ChartMeta(visible_fields=fields)


# ============================================================================
# Status text -> enum mapping (spec §B.1)
# ============================================================================

def _parse_status(status_text: Optional[str]) -> Optional[_ChartStatus]:
    """Map spec §B.1 status text to _ChartStatus.

    'Void' / '作废' / None -> VOID (point is kept, drawn as gray X, per issue #5
    user choice to make voids visible on the chart with the 4th style).
    Unknown labels fall through to IN_CONTROL (conservative default; not an
    error path).
    """
    if status_text is None:
        return _ChartStatus.VOID
    s = status_text.strip()
    # spec §B.1 status tokens: 'InControl', 'Warning(1_2s)', 'OutOfControl(...)',
    # 'Void' (and Chinese '作废' for legacy rows)
    if s in ("Void", "作废", "void"):
        return _ChartStatus.VOID
    if s.startswith("OutOfControl"):
        return _ChartStatus.OUT_OF_CONTROL
    if s.startswith("Warning"):
        return _ChartStatus.WARNING
    if s == "InControl":
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
    #    NOTE: per issue #5 user decision, void rows ARE included so the
    #    chart can render them as a 4th style (gray X). spec §B.4 still
    #    says voids are excluded from judgment / stats; that's the
    #    judgment layer's job, not the chart's. This module only reads.
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
        v = float(r["value"])
        # sd_score: spec §B.4 says voids do not participate in stats.
        # We still compute the deviation for display, but mark with NaN
        # to signal "not used in judgment" downstream consumers.
        if sd > 0 and st != _ChartStatus.VOID:
            sd_score = round((v - target) / sd, ROUND_PRECISION)
        elif st == _ChartStatus.VOID:
            sd_score = float("nan")
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
_COLOR_VOID = "gray"

_NO_DATA_MSG = "No Data"


def _build_title_lines(data: LJChartData, meta: ChartMeta) -> list[str]:
    """Compose chart title lines honoring meta.visible_fields (spec §E.2 f)."""
    v = meta.visible_fields
    lines: list[str] = []
    main_bits: list[str] = []

    if "project_name" in v and data.project_name:
        main_bits.append(data.project_name)
    if "test_item_name" in v and data.test_item_name:
        main_bits.append(data.test_item_name)
    if "level" in v and data.level:
        main_bits.append(f"Lv {data.level}")
    if main_bits:
        lines.append(" / ".join(main_bits))

    sub_bits: list[str] = []
    if "test_item_short" in v and data.test_item_short:
        sub_bits.append(f"short={data.test_item_short}")
    if "unit" in v and data.unit:
        sub_bits.append(f"unit={data.unit}")
    if "manufacturer" in v and data.manufacturer:
        sub_bits.append(f"mfr={data.manufacturer}")
    if "lot" in v and data.lot:
        sub_bits.append(f"lot={data.lot}")
    if "target" in v:
        sub_bits.append(f"target={data.target:.{ROUND_PRECISION}f}")
    if "sd" in v:
        sub_bits.append(f"sd={data.sd:.{ROUND_PRECISION}f}")
    if "operator" in v and data.operator:
        sub_bits.append(f"op={data.operator}")
    if "pre_processor" in v and data.pre_processor:
        sub_bits.append(f"prep={data.pre_processor}")
    if sub_bits:
        lines.append("    ".join(sub_bits))

    if "date_range" in v and data.date_range:
        lines.append(f"Range: {data.date_range[0]} ~ {data.date_range[1]}")

    if meta.notice:
        lines.append(meta.notice)

    return lines


def _empty_figure(title_lines: list[str], message: str) -> Figure:
    fig = Figure(figsize=(11, 6))
    ax = fig.add_subplot(111)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if title_lines:
        fig.suptitle("\n".join(title_lines), fontsize=11)
    ax.text(0.5, 0.5, message, ha="center", va="center",
            transform=ax.transAxes, fontsize=18, color="#888888")
    return fig


def render_lj_chart(
    data: LJChartData,
    *,
    meta: ChartMeta,
) -> Figure:
    """Render the Levey-Jennings chart.

    Returns a matplotlib.figure.Figure. Caller decides how to display / save it.
    """
    title_lines = _build_title_lines(data, meta)

    # Edge: no data -> placeholder figure
    if not data.points:
        return _empty_figure(title_lines, _NO_DATA_MSG)

    fig = Figure(figsize=(11, 6.5))
    ax = fig.add_subplot(111)

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

    # If SD=0, append notice and bail (only center line is drawn).
    if sd_zero:
        meta_local = ChartMeta(
            visible_fields=meta.visible_fields,
            notice=(meta.notice or "SD = 0, control limits invalid"),
        )
        title_lines = _build_title_lines(data, meta_local)
        fig.suptitle("\n".join(title_lines), fontsize=11)

        # Still plot the points so user sees they exist.
        for x, y, p in zip(xs, ys, data.points):
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
        fig.tight_layout()
        return fig

    # Plot points by status — 4 styles (issue #5 user decision):
    # in-control (black o), warning (yellow ^), out-of-control (red o),
    # void (gray X). Render each status in its own loop so the legend
    # entry per style still maps 1:1 to the actual markers on the chart.
    style_by_status = {
        _ChartStatus.IN_CONTROL:    ("o", _COLOR_IN_CTRL,  "InControl"),
        _ChartStatus.WARNING:       ("^", _COLOR_WARNING,  "Warning"),
        _ChartStatus.OUT_OF_CONTROL: ("o", _COLOR_OOC,      "OutOfControl"),
        _ChartStatus.VOID:          ("x", _COLOR_VOID,     "Void"),
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
    ax.set_title("\n".join(title_lines), fontsize=10)
    ax.set_xticks(xs)
    ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)

    # Y range: include ±3SD with a margin
    y_lo = min(ys) - 0.5 * sd
    y_hi = max(ys) + 0.5 * sd
    y_lo = min(y_lo, target - 3 * sd)
    y_hi = max(y_hi, target + 3 * sd)
    ax.set_ylim(y_lo, y_hi)

    ax.grid(True, alpha=0.25)

    # Custom legend (lines + 4 point types)
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
        Line2D([0], [0], marker="x", color="w",
               markerfacecolor=_COLOR_VOID, markeredgecolor=_COLOR_VOID,
               markersize=9, label="Void"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=9)
    fig.tight_layout()
    return fig


__all__ = [
    # data
    "LJChartPoint", "LJChartData", "ChartMeta",
    "ALL_FIELDS", "make_default_meta",
    # ops
    "compute_chart_data", "build_tooltip_text",
    # render
    "render_lj_chart",
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