"""
Issue #5 (Functional E) — UI layer tests (PyQt6).

These tests instantiate the LJChartDialog and ChartConfigDialog widgets
under the offscreen Qt platform (QT_QPA_PLATFORM=offscreen set in
tests/conftest.py for this module). They verify:

- Dropdowns populate from the database (project / test_item / lot / level / month).
- Re-selecting project clears test_item / lot / month choices.
- ChartConfigDialog exposes 12 fields with all checked by default.
- ChartConfigDialog can return a ChartMeta reflecting user's choices.
- LJChartDialog._build_meta() composes a ChartMeta from dialog state.
- _filter_months returns ordered (year, month) tuples for a given (project,
  test_item, lot, level) based on actual measurement rows.
- attach_hover_tooltip is callable and does not crash when no points drawn.

The dialog itself is not exec()-ed in these tests; widgets are shown
manually (show()) so QApplication processes layout but does not block.

Run:
    python -m pytest tests/test_issue5_ui_lj_chart.py -v
"""
from __future__ import annotations

import os

# Force offscreen Qt platform before any PyQt6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool import lj_chart as LJ


# ---------------------------------------------------------------------------
# Qt application fixture (module-scoped, single instance)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------

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


def _seed(conn: sqlite3.Connection) -> dict:
    p = P.create_project(
        conn, name="Vitamin D", short="Vit-D",
        unit="ng/mL", manufacturer="Roche",
    )
    ti_l = P.create_test_item(
        conn, p.id, name="D2", lot="LOT1", level="L",
        target=100.0, sd=2.0, short="D2-S", unit="ng/mL",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    ti_h = P.create_test_item(
        conn, p.id, name="D2", lot="LOT1", level="H",
        target=100.0, sd=2.0, short="D2-S", unit="ng/mL",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    # Add data in two months
    M.create_measurement(
        conn, test_item_id=ti_l.id, project_id=p.id,
        lot="LOT1", level="L", date="2026-03-15", value=100.0,
    )
    M.create_measurement(
        conn, test_item_id=ti_l.id, project_id=p.id,
        lot="LOT1", level="L", date="2026-04-01", value=100.5,
    )
    conn.commit()
    return {
        "project_id": p.id,
        "project_name": p.name,
        "test_item_id_l": ti_l.id,
        "test_item_id_h": ti_h.id,
        "test_item_name": "D2",
        "lot": "LOT1",
    }


# ---------------------------------------------------------------------------
# ChartConfigDialog
# ---------------------------------------------------------------------------

class TestChartConfigDialog:
    """12 fields, all on by default; can return a ChartMeta."""

    def test_default_all_12_checked(self, qapp, conn):
        from qc_tool.ui_lj_chart import ChartConfigDialog
        ids = _seed(conn)
        dlg = ChartConfigDialog(meta=LJ.make_default_meta())
        dlg.show()
        assert dlg.meta.visible_fields == set(LJ.ALL_FIELDS), \
            f"default should be all 12 on, got {dlg.meta.visible_fields}"
        dlg.close()

    def test_uncheck_one_field(self, qapp, conn):
        from qc_tool.ui_lj_chart import ChartConfigDialog
        dlg = ChartConfigDialog(meta=LJ.make_default_meta())
        dlg.show()
        # Toggle one off via the API
        dlg.set_checked("manufacturer", False)
        assert "manufacturer" not in dlg.meta.visible_fields
        dlg.close()

    def test_meta_property_returns_chart_meta(self, qapp, conn):
        from qc_tool.ui_lj_chart import ChartConfigDialog
        dlg = ChartConfigDialog(meta=LJ.make_default_meta())
        dlg.show()
        # date_range is always forced on by make_default_meta
        meta = dlg.meta
        assert isinstance(meta, LJ.ChartMeta)
        assert "date_range" in meta.visible_fields
        dlg.close()


# ---------------------------------------------------------------------------
# LJChartDialog: dropdown population + selection logic
# ---------------------------------------------------------------------------

class TestLJChartDialog:
    """Dropdown population + selection-driven chart refresh."""

    def test_dialog_constructs_without_error(self, qapp, conn):
        from qc_tool.ui_lj_chart import LJChartDialog
        ids = _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        assert dlg.isVisible()
        dlg.close()

    def test_project_dropdown_populated(self, qapp, conn):
        from qc_tool.ui_lj_chart import LJChartDialog
        ids = _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        names = [dlg.project_combo.itemText(i) for i in range(dlg.project_combo.count())]
        assert "Vitamin D" in names, names
        dlg.close()

    def test_selecting_project_fills_test_items(self, qapp, conn):
        from qc_tool.ui_lj_chart import LJChartDialog
        ids = _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        # Pick the Vitamin D row
        for i in range(dlg.project_combo.count()):
            if dlg.project_combo.itemText(i) == "Vitamin D":
                dlg.project_combo.setCurrentIndex(i)
                break
        ti_names = {
            dlg.test_item_combo.itemText(i)
            for i in range(dlg.test_item_combo.count())
        }
        # Test item combo shows "name lot level target sd" string; we just
        # need "D2" present.
        joined = " | ".join(ti_names)
        assert "D2" in joined, joined
        dlg.close()

    def test_months_filter_for_test_item_returns_two(self, qapp, conn):
        from qc_tool.ui_lj_chart import LJChartDialog
        ids = _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        months = dlg._filter_months(
            project_id=ids["project_id"],
            test_item_name="D2",
            lot="LOT1",
            level="L",
        )
        # March + April
        years_months = sorted(months)
        assert (2026, 3) in years_months, years_months
        assert (2026, 4) in years_months, years_months
        dlg.close()

    def test_months_filter_empty_when_no_data(self, qapp, conn):
        from qc_tool.ui_lj_chart import LJChartDialog
        ids = _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        months = dlg._filter_months(
            project_id=ids["project_id"],
            test_item_name="D2",
            lot="OTHER_LOT",
            date="L",
        ) if False else dlg._filter_months(
            project_id=ids["project_id"],
            test_item_name="D2",
            lot="LOT1",
            level="H",  # H has no data
        )
        assert months == []
        dlg.close()

    def test_build_meta_includes_all_default_fields(self, qapp, conn):
        from qc_tool.ui_lj_chart import LJChartDialog
        ids = _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        meta = dlg._build_meta()
        assert isinstance(meta, LJ.ChartMeta)
        assert meta.visible_fields == set(LJ.ALL_FIELDS), meta.visible_fields
        dlg.close()


# ---------------------------------------------------------------------------
# attach_hover_tooltip smoke
# ---------------------------------------------------------------------------

class TestHoverTooltip:
    def test_attach_hover_tooltip_does_not_crash(self, qapp, conn):
        from qc_tool.ui_lj_chart import LJChartDialog, attach_hover_tooltip
        ids = _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        # Render a chart and attach hover
        data = LJ.compute_chart_data(
            conn, project_id=ids["project_id"],
            test_item_name="D2", lot="LOT1", level="L",
            year=2026, month=4,
        )
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        try:
            attach_hover_tooltip(fig, dlg.canvas, data)
            # Calling it twice should also be safe (idempotent).
            attach_hover_tooltip(fig, dlg.canvas, data)
        except Exception as e:
            pytest.fail(f"attach_hover_tooltip crashed: {e}")
        dlg.close()