"""
Issue #5 (Functional E: Levey-Jennings QC Chart) — test-first
(spec V1.3 §E.1-E.2 + §10 boundary)

Per iron rule 5: "judgment logic must be test-first" — judgment layer
is already in §D; this issue only reads / plots.

Test organization:
- TestComputeChartData    : data-layer interface contract
- TestRenderBasic         : a (7 lines) + b (3 colors / 3 markers)
- TestRenderVoid          : b (void rows not drawn)
- TestRenderSdZero        : SD=0 boundary
- TestRenderNoData        : no data / selected month out of range
- TestRenderFieldsVisibility : f (12 fields visible/hidden)
- TestRenderLinesCount    : a (7 lines)
- TestRenderLegend        : e (legend items)
- TestRenderRulesTooltip  : c (tooltip text payload)

Run:
    python -m pytest tests/test_issue5_lj_chart.py -v
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool import lj_chart as LJ  # to be implemented


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "qc.db"
    init_database(db)
    c = get_connection(db)
    c.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, 'Test Lab')")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _seed_basic(
    conn: sqlite3.Connection,
    *,
    project: str = "Vitamin D",
    name: str = "D2",
    lot: str = "LOT1",
    target: float = 100.0,
    sd: float = 2.0,
) -> dict:
    """Create project + L + H level; return id dict."""
    p = P.create_project(
        conn, name=project, short="Vit-D",
        unit="ng/mL", manufacturer="Roche",
    )
    ti_l = P.create_test_item(
        conn, p.id, name=name, lot=lot, level="L",
        target=target, sd=sd, short="D2-S", unit="ng/mL",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    ti_h = P.create_test_item(
        conn, p.id, name=name, lot=lot, level="H",
        target=target, sd=sd, short="D2-S", unit="ng/mL",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    conn.commit()
    return {
        "project_id": p.id,
        "project_name": p.name,
        "test_item_id_l": ti_l.id,
        "test_item_id_h": ti_h.id,
    }


def _seed_with_status(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    project_id: int,
    date: str,
    value: float,
    level: str,
    lot: str,
    status: str,
    rules: list[str],
    seq: int = 0,
) -> int:
    """Insert a measurement with explicit status/rules (bypass judgment)."""
    cur = conn.execute(
        "INSERT INTO measurements("
        "  test_item_id, project_id, lot, level, date, seq, value,"
        "  status, rules"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            test_item_id, project_id, lot, level, date, seq, value,
            status,
            json.dumps(rules, ensure_ascii=False) if rules else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_void(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    project_id: int,
    date: str,
    value: float,
    level: str = "L",
    lot: str = "LOT1",
) -> int:
    """Insert a void row."""
    cur = conn.execute(
        "INSERT INTO measurements("
        "  test_item_id, project_id, lot, level, date, seq, value,"
        "  status, rules"
        ") VALUES (?, ?, ?, ?, ?, 0, ?, 'Void', NULL)",
        (test_item_id, project_id, lot, level, date, value),
    )
    conn.commit()
    return int(cur.lastrowid)


def _figure_text_blob(fig: Any) -> str:
    """Collect every text inside figure (titles + annotations)."""
    parts = []
    supt = fig._suptitle
    if supt is not None:
        parts.append(supt.get_text())
    for ax in fig.axes:
        t = ax.get_title()
        if t:
            parts.append(t)
        for txt in ax.texts:
            parts.append(str(txt.get_text()))
    return " | ".join(parts)


def _chart_data(conn, *, project_id, name="D2", lot="LOT1", level="L",
                year=2026, month=4) -> Any:
    return LJ.compute_chart_data(
        conn, project_id=project_id, test_item_name=name,
        lot=lot, level=level, year=year, month=month,
    )


# ============================================================================
# Data layer
# ============================================================================

class TestComputeChartData:
    """compute_chart_data: business-layer entry."""

    def test_interface_has_all_required_fields(self, conn):
        ids = _seed_basic(conn)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        for attr in (
            "points", "target", "sd", "project_name", "test_item_name",
            "test_item_short", "unit", "manufacturer", "lot", "level",
            "operator", "pre_processor", "year", "month", "date_range",
        ):
            assert hasattr(data, attr), f"LJChartData missing {attr}"

    def test_month_boundary_only_target_month(self, conn):
        ids = _seed_basic(conn)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-15", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-03-15", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(conn, project_id=ids["project_id"], month=4)
        dates = sorted({p.date for p in data.points})
        assert dates == ["2026-04-15"], dates

    def test_void_rows_excluded_from_chart_data(self, conn):
        """spec §E.1（2026-10-12 用户确认）：作废点不显示，图表数据层排除作废行。"""
        ids = _seed_basic(conn)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        _seed_void(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-02", value=101.0, level="L", lot="LOT1",
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        dates = sorted({p.date for p in data.points})
        assert dates == ["2026-04-01"], dates
        # 作废行不再进入图表数据
        void_pts = [p for p in data.points if p.status == LJ._ChartStatus.VOID]
        assert len(void_pts) == 0

    def test_name_or_lot_mismatch_returns_empty(self, conn):
        ids = _seed_basic(conn, lot="LOT1")
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(
            conn, project_id=ids["project_id"], lot="OTHER_LOT",
        )
        assert data.points == []

    def test_level_mismatch_returns_empty(self, conn):
        ids = _seed_basic(conn)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        # Query level=H but only L has data
        data = _chart_data(conn, project_id=ids["project_id"], level="H")
        assert data.points == []

    def test_sd_score_value(self, conn):
        """(value - target) / sd -> rounded to 3dp (spec §D.5)."""
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=107.0, level="L", lot="LOT1",
            status="OutOfControl(1_3s)", rules=["1_3s"], seq=0,
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        assert len(data.points) == 1
        p = data.points[0]
        assert abs(p.sd_score - 3.5) < 0.001, p.sd_score

    def test_rules_parsed_from_json(self, conn):
        ids = _seed_basic(conn)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=107.0, level="L", lot="LOT1",
            status="OutOfControl(1_3s + 2_2s)", rules=["1_3s", "2_2s"], seq=0,
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        p = data.points[0]
        assert set(p.rules) == {"1_3s", "2_2s"}

    def test_date_range_month_first_last_day(self, conn):
        ids = _seed_basic(conn)
        data = _chart_data(conn, project_id=ids["project_id"], year=2026, month=4)
        assert data.date_range == ("2026-04-01", "2026-04-30"), data.date_range

    def test_no_data_returns_empty_points_but_meta_filled(self, conn):
        ids = _seed_basic(conn)
        data = _chart_data(conn, project_id=ids["project_id"])
        assert data.points == []
        assert data.target == 100.0
        assert data.sd == 2.0

    def test_1000_rows_same_day_no_aggregation(self, conn):
        """Same day, 1000 different seq -> all 1000 kept (no aggregation)."""
        ids = _seed_basic(conn)
        for i in range(1000):
            _seed_with_status(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"],
                date="2026-04-01", value=100.0 + (i % 3) * 0.1,
                level="L", lot="LOT1", status="InControl", rules=[], seq=i,
            )
        data = _chart_data(conn, project_id=ids["project_id"])
        assert len(data.points) == 1000


# ============================================================================
# Render layer
# ============================================================================

class TestRenderBasic:
    """a (7 lines) + b (3 colors / 3 markers)."""

    def test_seven_hlines_covering_target_pm_3sd(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        for d in ("2026-04-01", "2026-04-02", "2026-04-03"):
            _seed_with_status(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"],
                date=d, value=100.0, level="L", lot="LOT1",
                status="InControl", rules=[],
            )
        data = _chart_data(conn, project_id=ids["project_id"])
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        ax = fig.axes[0]
        # axhline lines carry labels like 'Center', '+1SD', '-1SD', '+2SD', etc.
        labels = {
            ln.get_label() for ln in ax.get_lines()
            if ln.get_label() and ln.get_label() != "_"
        }
        for need in ("Center", "+1SD", "-1SD", "+2SD", "-2SD", "+3SD", "-3SD"):
            assert need in labels, f"missing {need!r} in {labels!r}"
        y_min, y_max = ax.get_ylim()
        assert y_min <= 100.0 - 3 * 2.0
        assert y_max >= 100.0 + 3 * 2.0

    def test_three_colors_and_markers_present(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[], seq=0,
        )
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-02", value=104.1, level="L", lot="LOT1",
            status="Warning(1_2s)", rules=["1_2s"], seq=1,
        )
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-03", value=107.0, level="L", lot="LOT1",
            status="OutOfControl(1_3s)", rules=["1_3s"], seq=2,
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        ax = fig.axes[0]
        colors = set()
        markers = set()
        for ln in ax.get_lines():
            m = ln.get_marker()
            if m in ("o", "^", "v", "s", "D", "x", "P"):
                markers.add(m)
                c = ln.get_color()
                if isinstance(c, (list, tuple)) and len(c) in (3, 4):
                    colors.add(tuple(round(float(v), 2) for v in c))
                else:
                    colors.add(c)
        assert len(colors) >= 3, colors
        assert len(markers) >= 2, markers


class TestRenderVoid:
    """spec §E.1（2026-10-12 用户确认）：作废点不显示。"""

    def test_void_row_not_drawn(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        _seed_void(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-02", value=107.0, level="L", lot="LOT1",
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        # 作废行已从图表数据排除，仅剩在控行
        assert {p.date for p in data.points} == {"2026-04-01"}
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        ax = fig.axes[0]
        # Count scatter markers (any non-line) — 只有 1 个（在控），无灰色 X
        marker_count = sum(
            1 for ln in ax.get_lines()
            if ln.get_marker() in ("o", "^", "x", "v", "s", "D", "P")
            and ln.get_label() not in ("Center", "+1SD", "-1SD", "+2SD",
                                        "-2SD", "+3SD", "-3SD")
        )
        assert marker_count == 1, marker_count
        # 无 'x' 标记，且图例不含 Void
        assert not any(ln.get_marker() == "x" for ln in ax.get_lines()), \
            "作废点不应以灰色 X 出现在图上"
        legend_texts = [t.get_text() for t in ax.get_legend().get_texts()]
        assert "Void" not in legend_texts


class TestRenderSdZero:
    """SD=0 -> only centerline + 'SD = 0, control limits invalid' notice."""

    def test_sd_zero_only_centerline(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=0.0001)
        conn.execute("UPDATE test_items SET sd = 0 WHERE id = ?", (ids["test_item_id_l"],))
        conn.commit()
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        assert data.sd == 0.0, data.sd
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        blob = _figure_text_blob(fig)
        assert "SD = 0" in blob or "SD=0" in blob, blob


class TestRenderNoData:
    """No data / selected month out of range."""

    def test_no_data_renders_placeholder(self, conn):
        ids = _seed_basic(conn)
        data = _chart_data(conn, project_id=ids["project_id"])
        assert data.points == []
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        blob = _figure_text_blob(fig)
        # Spec copy: "暂无数据" or "该月无数据"
        assert ("No Data" in blob) or ("NoData" in blob), blob

    def test_month_out_of_range_empty_points(self, conn):
        ids = _seed_basic(conn)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(conn, project_id=ids["project_id"], month=5)
        assert data.points == []


class TestRenderFieldsVisibility:
    """f: 新标题格式字段可见性（用户拍板 2026-10-13：6 字段 + 时间范围恒显）。"""

    @pytest.fixture
    def data_with_meta(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        return _chart_data(conn, project_id=ids["project_id"])

    def test_default_shows_all_title_fields(self, data_with_meta):
        fig = LJ.render_lj_chart(data_with_meta, meta=LJ.make_default_meta())
        blob = _figure_text_blob(fig)
        for kw in ("Vitamin D质控图", "质控品批号：LOT1", "靶值（L）=100.000",
                   "SD（L）=2.000", "仪器操作：Op-1", "前处理：Pre-1",
                   "时间范围：2026-04-01~2026-04-30"):
            assert kw in blob, f"missing {kw!r} in {blob!r}"
        # 首行「项目名质控图」独立大字号（用户需求：×2）
        assert fig._suptitle.get_text() == "Vitamin D质控图"
        assert fig._suptitle.get_fontsize() == 18
        # 新格式不再显示指标名/简称/单位/厂家
        for kw in ("D2", "D2-S", "ng/mL", "Roche"):
            assert kw not in blob, f"unexpected {kw!r} in {blob!r}"

    def test_all_off_keeps_only_date_range_fallback(self, data_with_meta):
        fig = LJ.render_lj_chart(
            data_with_meta, meta=LJ.make_default_meta(visible_fields=set()),
        )
        blob = _figure_text_blob(fig)
        for kw in ("100.000", "2.000", "Roche", "LOT1"):
            assert kw not in blob, f"unexpected {kw!r} in {blob!r}"
        assert "时间范围：2026-04-01~2026-04-30" in blob

    def test_single_field_off(self, data_with_meta):
        meta = LJ.make_default_meta()
        meta.visible_fields.discard("lot")
        fig = LJ.render_lj_chart(data_with_meta, meta=meta)
        blob = _figure_text_blob(fig)
        assert "质控品批号" not in blob
        assert "靶值（L）=100.000" in blob


class TestRenderLinesCount:
    """a: 7 lines (center + +-1 +-2 +-3 SD)."""

    def test_seven_horizontal_lines(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        ax = fig.axes[0]
        labels = {
            ln.get_label() for ln in ax.get_lines()
            if ln.get_label() and ln.get_label() != "_"
        }
        for need in ("Center", "+1SD", "-1SD", "+2SD", "-2SD", "+3SD", "-3SD"):
            assert need in labels, f"missing {need!r} in {labels!r}"


class TestRenderLegend:
    """e: legend contains each line type and point type."""

    def test_legend_has_center_and_pm_lines(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        ax = fig.axes[0]
        # Pull labels from the rendered Legend object directly — handles
        # provided via ax.legend(handles=...) are stored on the Legend,
        # not in ax.get_lines().
        legend = ax.get_legend()
        assert legend is not None, "no legend rendered"
        labels = [t.get_text() for t in legend.get_texts()]
        text = " | ".join(labels)
        for kw in ("Center", "+1SD", "+2SD", "+3SD", "InControl", "Warning", "OutOfControl"):
            assert kw in text, f"missing legend {kw!r}; labels={text!r}"


class TestRenderRulesTooltip:
    """c: tooltip payload for in-control vs warning/out-of-control."""

    def test_out_of_control_tooltip_has_rules(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=107.0, level="L", lot="LOT1",
            status="OutOfControl(1_3s + 2_2s)", rules=["1_3s", "2_2s"],
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        p = data.points[0]
        text = LJ.build_tooltip_text(p)
        assert "2026-04-01" in text
        assert "107.0" in text or "107" in text
        assert "1_3s" in text
        assert "2_2s" in text

    def test_in_control_tooltip_no_rules_list(self, conn):
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        _seed_with_status(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            date="2026-04-01", value=100.0, level="L", lot="LOT1",
            status="InControl", rules=[],
        )
        data = _chart_data(conn, project_id=ids["project_id"])
        p = data.points[0]
        text = LJ.build_tooltip_text(p)
        assert "2026-04-01" in text
        assert "100.0" in text or "100" in text
        assert "1_3s" not in text
        assert "1_2s" not in text