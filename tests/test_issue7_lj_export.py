"""
Issue #7 (Functional G.3: PNG export of Levey-Jennings chart)

Test target: qc_tool.lj_chart.save_png()

Per AGENTS.md iron rule 6: don't create duplicate files. The save_png
method is an addition to lj_chart.py (no new module).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import lj_chart as LJ


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "qc.db"
    init_database(db)
    c = get_connection(db)
    c.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, '测试实验室')")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _seed_basic(conn: sqlite3.Connection) -> dict:
    p = P.create_project(conn, name="维生素D", unit="ng/mL")
    ti = P.create_test_item(
        conn, p.id, name="D2", lot="LOT1", level="L",
        target=100.0, sd=2.0,
    )
    for d in ("2026-04-01", "2026-04-02", "2026-04-03"):
        conn.execute(
            "INSERT INTO measurements("
            "  test_item_id, project_id, lot, level, date, value, status, rules"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ti.id, p.id, "LOT1", "L", d, 100.0, "InControl", None),
        )
    conn.commit()
    return {"project_id": p.id, "test_item_id": ti.id, "name": "D2"}


class TestLjChartSavePng:
    """G.3: 质控图页点'导出图片'→ 保存当前质控图为 PNG。"""

    def test_save_png_creates_file(self, conn, tmp_path):
        ids = _seed_basic(conn)
        data = LJ.compute_chart_data(
            conn, project_id=ids["project_id"],
            test_item_name=ids["name"], lot="LOT1", level="L",
            year=2026, month=4,
        )
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        out = tmp_path / "chart.png"
        LJ.save_png(fig, out, dpi=300)
        assert out.exists()
        assert out.stat().st_size > 1000  # not empty

    def test_save_png_returns_bytes(self, conn):
        ids = _seed_basic(conn)
        data = LJ.compute_chart_data(
            conn, project_id=ids["project_id"],
            test_item_name=ids["name"], lot="LOT1", level="L",
            year=2026, month=4,
        )
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        # New API: also accept to_bytes() for tests
        png_bytes = LJ.save_png_to_bytes(fig, dpi=300)
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(png_bytes) > 1000

    def test_save_png_default_dpi_300(self, conn, tmp_path):
        """spec G.3 + AGENTS.md §11: 默认 DPI=300 (≥300)。"""
        ids = _seed_basic(conn)
        data = LJ.compute_chart_data(
            conn, project_id=ids["project_id"],
            test_item_name=ids["name"], lot="LOT1", level="L",
            year=2026, month=4,
        )
        fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        # Default dpi kwarg
        out = tmp_path / "chart2.png"
        LJ.save_png(fig, out)  # no dpi → use default
        assert out.exists()
        # file size at 300 dpi is > at 120 dpi
        size_300 = out.stat().st_size

    def test_save_png_respects_meta_visibility(self, conn, tmp_path):
        """spec G.3: 导出时元信息是否显示由'图表配置'决定。"""
        ids = _seed_basic(conn)
        data = LJ.compute_chart_data(
            conn, project_id=ids["project_id"],
            test_item_name=ids["name"], lot="LOT1", level="L",
            year=2026, month=4,
        )
        # All fields hidden (only date_range fallback) → smaller title text
        meta_hidden = LJ.make_default_meta(visible_fields=set())
        fig_hidden = LJ.render_lj_chart(data, meta=meta_hidden)
        out_hidden = tmp_path / "hidden.png"
        LJ.save_png(fig_hidden, out_hidden, dpi=120)
        assert out_hidden.exists()
        # And the default all-visible meta
        fig_full = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
        out_full = tmp_path / "full.png"
        LJ.save_png(fig_full, out_full, dpi=120)
        assert out_full.exists()
