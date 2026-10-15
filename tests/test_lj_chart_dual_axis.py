"""
用户需求（2026-10-12 第二次补充）：质控图改为一图双 y 轴（取代并排双图）。

- 左 y 轴 = L 水平测定值，右 y 轴 = H 水平测定值；
- 两侧 7 条控制限（靶值绿实线、±1SD 绿虚线、±2SD 黄虚线、±3SD 红实线）
  必须像素级重合；
- L 点：在控绿实心圆 / 警告黄实心圆 / 失控红实心圆；
- H 点：在控蓝实心三角 / 警告黄实心三角 / 失控红实心三角；
- 横轴同日 L/H 逐点配对同 x：第 i 个 L 点与第 i 个 H 点共用一横位
  （2026-10-13 用户第四次调整）；
- 单水平图样式保持 spec §E.1 不变（回归）。

Run:
    python -m pytest tests/test_lj_chart_dual_axis.py -v
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from matplotlib import colors as mcolors

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import lj_chart as LJ

GREEN = "#1f7a1f"
YELLOW = "#d9a400"
RED = "#c0392b"
BLUE = "#1f5fa3"

_CONTROL_LABELS = {"-3SD", "-2SD", "-1SD", "Center", "+1SD", "+2SD", "+3SD"}


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


def _seed_pair(
    conn: sqlite3.Connection,
    *,
    target_l: float = 10.0,
    sd_l: float = 1.0,
    target_h: float = 50.0,
    sd_h: float = 5.0,
) -> dict:
    """项目 + 同名同批号 L/H 两水平；返回 id dict。"""
    p = P.create_project(
        conn, name="生化全套", short="SH",
        unit="U/L", manufacturer="Roche",
    )
    ti_l = P.create_test_item(
        conn, p.id, name="ALT", lot="LOT2026A", level="L",
        target=target_l, sd=sd_l, short="ALT-S", unit="U/L",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    ti_h = P.create_test_item(
        conn, p.id, name="ALT", lot="LOT2026A", level="H",
        target=target_h, sd=sd_h, short="ALT-S", unit="U/L",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    conn.commit()
    return {
        "project_id": p.id,
        "test_item_id_l": ti_l.id,
        "test_item_id_h": ti_h.id,
    }


def _insert_measurement(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    project_id: int,
    date: str,
    value: float,
    level: str,
    lot: str = "LOT2026A",
    status: str = "InControl",
    rules: str = None,
    seq: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO measurements("
        "  test_item_id, project_id, lot, level, date, seq, value,"
        "  status, rules"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (test_item_id, project_id, lot, level, date, seq, value, status, rules),
    )
    conn.commit()


def _chart_data(conn, ids, *, level, year=2026, month=5):
    return LJ.compute_chart_data(
        conn, project_id=ids["project_id"],
        test_item_name="ALT", lot="LOT2026A", level=level,
        year=year, month=month,
    )


def _count_control_lines(ax) -> int:
    """统计 ax 上的控制限横线（label 为 ±nSD / Center）。"""
    return sum(1 for ln in ax.get_lines() if ln.get_label() in _CONTROL_LABELS)


def _point_styles(ax) -> dict:
    """{(marker, facecolor_hex): count}，仅统计数据点线（排除 axhline）。"""
    out: dict = {}
    for ln in ax.get_lines():
        if ln.get_label() in _CONTROL_LABELS:
            continue
        m = ln.get_marker()
        if not m or m == "None":
            continue
        fc = mcolors.to_hex(ln.get_markerfacecolor(), keep_alpha=False)
        out[(m, fc)] = out.get((m, fc), 0) + 1
    return out


# ============================================================================
# render_lj_chart_dual_axis
# ============================================================================

class TestRenderDualAxis:
    def test_two_axes_left_l_right_h(self, conn):
        """一图双轴：左轴围绕 L 靶值、右轴围绕 H 靶值（量级不同也成立）。"""
        ids = _seed_pair(conn)
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.5, level="L")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-01",
            value=51.0, level="H")

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        assert len(fig.axes) == 2, "双轴图应有 2 个 axes（twinx）"
        ax_l, ax_h = fig.axes
        yl_l, yl_h = ax_l.get_ylim(), ax_h.get_ylim()
        assert yl_l[0] <= 10.0 <= yl_l[1]
        assert yl_l[1] < 20.0, "左轴应围绕 L 靶值 10，而非 H 靶值 50"
        assert yl_h[0] <= 50.0 <= yl_h[1]
        assert yl_h[1] > 60.0, "右轴应围绕 H 靶值 50"
        assert "L" in ax_l.get_ylabel()
        assert "H" in ax_h.get_ylabel()
        title = ax_l.get_title()
        assert "质控品批号：LOT2026A" in title
        assert "靶值（L）=10.000" in title
        assert "靶值（H）=50.000" in title
        # 首行「项目名质控图」独立大字号（用户需求：×2）
        assert fig._suptitle.get_text() == "生化全套质控图"
        assert fig._suptitle.get_fontsize() == 18

    def test_title_same_fields_one_line_diff_fields_two_lines(self, conn):
        """新标题格式（2026-10-13）：L/H 同值一行，不同值按（L）（H）分两行。"""
        # 相同批号/仪器操作/前处理 → 单行
        ids = _seed_pair(conn)
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.5, level="L")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-01",
            value=51.0, level="H")
        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        title = fig.axes[0].get_title()
        assert "质控品批号：LOT2026A" in title
        assert "仪器操作：Op-1" in title
        assert "前处理：Pre-1" in title

        # 批号/仪器操作/前处理不同 → （L）（H）两行
        p = P.create_project(conn, name="差批项目", short="SH",
                             unit="U/L", manufacturer="Roche")
        ti_l2 = P.create_test_item(
            conn, p.id, name="ALT", lot="LOTA", level="L",
            target=10.0, sd=1.0, operator="李", pre_processor="张三",
        )
        ti_h2 = P.create_test_item(
            conn, p.id, name="ALT", lot="LOTB", level="H",
            target=50.0, sd=5.0, operator="王", pre_processor="李四",
        )
        conn.commit()
        _insert_measurement(
            conn, test_item_id=ti_l2.id, project_id=p.id,
            date="2026-05-01", value=10.5, level="L", lot="LOTA")
        _insert_measurement(
            conn, test_item_id=ti_h2.id, project_id=p.id,
            date="2026-05-01", value=51.0, level="H", lot="LOTB")
        fig2 = LJ.render_lj_chart_dual_axis(
            LJ.compute_chart_data(
                conn, project_id=p.id, test_item_name="ALT",
                lot="LOTA", level="L", year=2026, month=5),
            LJ.compute_chart_data(
                conn, project_id=p.id, test_item_name="ALT",
                lot="LOTB", level="H", year=2026, month=5),
            meta=LJ.make_default_meta(),
        )
        title2 = fig2.axes[0].get_title()
        assert "质控品批号（L）：LOTA" in title2
        assert "质控品批号（H）：LOTB" in title2
        assert "仪器操作（L）：李" in title2
        assert "仪器操作（H）：王" in title2
        assert "前处理（L）：张三" in title2
        assert "前处理（H）：李四" in title2
        assert "时间范围：2026-05-01~2026-05-31" in title2

    def test_control_lines_coincide_pixel_wise(self, conn):
        """左右两侧 7 条控制限必须像素级重合（靶值/SD 故意取不同值）。"""
        ids = _seed_pair(conn)
        for d, v in [("2026-05-01", 10.5), ("2026-05-02", 9.8)]:
            _insert_measurement(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"], date=d, value=v, level="L")
        for d, v in [("2026-05-01", 51.0), ("2026-05-02", 48.0)]:
            _insert_measurement(
                conn, test_item_id=ids["test_item_id_h"],
                project_id=ids["project_id"], date=d, value=v, level="H")

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        fig.canvas.draw()
        ax_l, ax_h = fig.axes

        # 两侧各自 7 条控制限
        assert _count_control_lines(ax_l) == 7
        assert _count_control_lines(ax_h) == 7

        # 同一 k·SD 线在左右两轴落到同一像素高度（重合断言）
        for k in (-3, -2, -1, 0, 1, 2, 3):
            py_l = ax_l.transData.transform((0.5, 10.0 + k * 1.0))[1]
            py_h = ax_h.transData.transform((0.5, 50.0 + k * 5.0))[1]
            assert py_l == pytest.approx(py_h, abs=1e-6), f"k={k} 应重合"

        # 线色线型：中心绿实线、±1SD 绿虚线、±2SD 黄虚线、±3SD 红实线
        lines_by_label = {ln.get_label(): ln for ln in ax_l.get_lines()
                          if ln.get_label() in _CONTROL_LABELS}
        for lbl, color, ls in [
            ("Center", GREEN, "-"),
            ("+1SD", GREEN, "--"), ("-1SD", GREEN, "--"),
            ("+2SD", YELLOW, "--"), ("-2SD", YELLOW, "--"),
            ("+3SD", RED, "-"), ("-3SD", RED, "-"),
        ]:
            ln = lines_by_label[lbl]
            assert mcolors.to_hex(ln.get_color(), keep_alpha=False) == color, lbl
            assert ln.get_linestyle() == ls, lbl

    def test_point_styles_by_level_and_status(self, conn):
        """六种点样式：L 绿/黄/红圆，H 蓝/黄/红三角。

        状态字串用 spec §B.1 中文 token（判定层实际写入格式），
        回归 _parse_status 中文映射。
        """
        ids = _seed_pair(conn)
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.2, level="L", status="在控")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-02",
            value=12.2, level="L", status="警告(1_2s)",
            rules='["1_2s"]')
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-03",
            value=13.5, level="L", status="失控(1_3s)",
            rules='["1_3s"]')
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-01",
            value=51.0, level="H", status="在控")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-02",
            value=61.0, level="H", status="警告(1_2s)",
            rules='["1_2s"]')
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-03",
            value=67.0, level="H", status="失控(1_3s)",
            rules='["1_3s"]')

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        ax_l, ax_h = fig.axes
        assert _point_styles(ax_l) == {
            ("o", GREEN): 1, ("o", YELLOW): 1, ("o", RED): 1,
        }, "L：在控绿圆 / 警告黄圆 / 失控红圆"
        assert _point_styles(ax_h) == {
            ("^", BLUE): 1, ("^", YELLOW): 1, ("^", RED): 1,
        }, "H：在控蓝三角 / 警告黄三角 / 失控红三角"

    def test_shared_x_aligned_by_date(self, conn):
        """横轴每日期一列：同日 L/H 严格同一 x（2026-10-13 用户第三次调整）。"""
        ids = _seed_pair(conn)
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.5, level="L")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-03",
            value=9.8, level="L")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-01",
            value=51.0, level="H")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-02",
            value=48.0, level="H")

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        ax_l, ax_h = fig.axes
        xs_l = sorted({ln.get_xdata()[0] for ln in ax_l.get_lines()
                       if ln.get_marker() == "o"})
        xs_h = sorted({ln.get_xdata()[0] for ln in ax_h.get_lines()
                       if ln.get_marker() == "^"})
        assert xs_l == [1.0, 3.0], "L：05-01、05-03 → 横位第 1、3 位"
        assert xs_h == [1.0, 2.0], "H：05-01、05-02 → 横位第 1、2 位"
        # 同日 05-01 的 L 与 H 同一 x（配对对齐）
        assert 1.0 in xs_l and 1.0 in xs_h

    def test_same_day_multi_points_pairwise_aligned(self, conn):
        """同日多条：第 i 个 L 点与第 i 个 H 点共用同一 x（2026-10-13 第四次调整）。"""
        ids = _seed_pair(conn)
        for v in (10.1, 10.5, 9.8):
            _insert_measurement(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"], date="2026-05-01",
                value=v, level="L")
        for v in (51.0, 49.0, 52.0):
            _insert_measurement(
                conn, test_item_id=ids["test_item_id_h"],
                project_id=ids["project_id"], date="2026-05-01",
                value=v, level="H")

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        ax_l, ax_h = fig.axes
        xs_l = sorted(ln.get_xdata()[0] for ln in ax_l.get_lines()
                      if ln.get_marker() == "o")
        xs_h = sorted(ln.get_xdata()[0] for ln in ax_h.get_lines()
                      if ln.get_marker() == "^")
        assert xs_l == [1.0, 2.0, 3.0], xs_l
        assert xs_h == [1.0, 2.0, 3.0], "逐点配对：H 应与 L 完全同 x"
        # 日期刻度居该组中点
        labels = [t.get_text() for t in ax_l.get_xticklabels()]
        assert labels == ["2026-05-01"], labels
        assert float(ax_l.get_xticks()[0]) == 2.0, ax_l.get_xticks()

    def test_one_side_sd_zero_center_coincides(self, conn):
        """一侧 SD=0：该侧仅中心线，且与对侧中心线像素重合 + 标题提示。"""
        ids = _seed_pair(conn)
        conn.execute(
            "UPDATE test_items SET sd = 0 WHERE id = ?",
            (ids["test_item_id_h"],),
        )
        conn.commit()
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.5, level="L")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-01",
            value=50.0, level="H")

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        fig.canvas.draw()
        ax_l, ax_h = fig.axes
        # H 侧（SD=0）仅 1 条中心线；L 侧仍 7 条
        assert _count_control_lines(ax_l) == 7
        assert _count_control_lines(ax_h) == 1
        center_h = [ln for ln in ax_h.get_lines() if ln.get_label() == "Center"]
        assert len(center_h) == 1
        # 中心线重合（两侧 ylim 均对称于各自靶值）
        py_l = ax_l.transData.transform((0.5, 10.0))[1]
        py_h = ax_h.transData.transform((0.5, 50.0))[1]
        assert py_l == pytest.approx(py_h, abs=1e-6)
        # 标题含 SD=0 提示
        assert "SD = 0" in ax_l.get_title()

    def test_one_side_no_points(self, conn):
        """某水平当月无点：该侧只画控制限、无数据点，不崩溃。"""
        ids = _seed_pair(conn)
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.5, level="L")

        data_l = _chart_data(conn, ids, level="L")
        data_h = _chart_data(conn, ids, level="H")
        assert data_h.points == []
        fig = LJ.render_lj_chart_dual_axis(data_l, data_h,
                                           meta=LJ.make_default_meta())
        ax_l, ax_h = fig.axes
        assert _count_control_lines(ax_h) == 7
        assert _point_styles(ax_h) == {}
        assert _point_styles(ax_l) == {("o", GREEN): 1}

    def test_both_empty_no_data(self, conn):
        """两水平均无数据 → No Data 占位图。"""
        ids = _seed_pair(conn)
        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        assert len(fig.axes) == 1
        texts = [t.get_text() for t in fig.axes[0].texts]
        assert any("No Data" in t for t in texts), texts

    def test_dual_axis_png_export(self, conn):
        """双轴图可导出 PNG bytes（G.3 质量档）。"""
        ids = _seed_pair(conn)
        for d, v in [("2026-05-01", 10.5), ("2026-05-02", 9.8)]:
            _insert_measurement(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"], date=d, value=v, level="L")
            _insert_measurement(
                conn, test_item_id=ids["test_item_id_h"],
                project_id=ids["project_id"], date=d, value=51.0, level="H")

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        png = LJ.save_png_to_bytes(fig, dpi=150)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(png) > 10000


# ============================================================================
# _parse_status 状态字串映射（spec §B.1 中文 token + 英文兼容）
# ============================================================================

class TestParseStatus:
    def test_parse_status_chinese_and_english(self):
        """spec §B.1 中文状态字串必须正确映射；英文 token 保持兼容。"""
        assert LJ._parse_status("在控") == LJ._ChartStatus.IN_CONTROL
        assert LJ._parse_status("警告(1_2s)") == LJ._ChartStatus.WARNING
        assert LJ._parse_status("失控(1_3s + 4_1s)") == LJ._ChartStatus.OUT_OF_CONTROL
        assert LJ._parse_status("作废") == LJ._ChartStatus.VOID
        assert LJ._parse_status("InControl") == LJ._ChartStatus.IN_CONTROL
        assert LJ._parse_status("Warning(1_2s)") == LJ._ChartStatus.WARNING
        assert LJ._parse_status("OutOfControl(2_2s)") == LJ._ChartStatus.OUT_OF_CONTROL
        assert LJ._parse_status("Void") == LJ._ChartStatus.VOID
        assert LJ._parse_status(None) == LJ._ChartStatus.VOID
        # 未知字串保守按在控处理（非错误路径）
        assert LJ._parse_status("未知状态") == LJ._ChartStatus.IN_CONTROL


class TestDualAxisSpanCap:
    def test_extreme_outlier_capped_at_4sd_pinned(self, conn):
        """极端离群点不撑爆 y 轴：可见跨度封顶 ±4SD，离群点钉边 + 标注。"""
        ids = _seed_pair(conn)  # L: 10/1, H: 50/5
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.2, level="L", status="在控")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-02",
            value=1000.0, level="L", status="失控(1_3s)")  # ≈ +990SD
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"], date="2026-05-01",
            value=51.0, level="H", status="在控")

        fig = LJ.render_lj_chart_dual_axis(
            _chart_data(conn, ids, level="L"),
            _chart_data(conn, ids, level="H"),
            meta=LJ.make_default_meta(),
        )
        fig.canvas.draw()
        ax_l, ax_h = fig.axes

        # 1. 跨度封顶：ylim = 靶值 ± 4×SD（而非被离群点撑到 ±990SD）
        assert ax_l.get_ylim() == pytest.approx((10.0 - 4 * 1.0, 10.0 + 4 * 1.0))
        assert ax_h.get_ylim() == pytest.approx((50.0 - 4 * 5.0, 50.0 + 4 * 5.0))

        # 2. 控制限仍像素重合
        for k in (-3, 0, 3):
            py_l = ax_l.transData.transform((0.5, 10.0 + k * 1.0))[1]
            py_h = ax_h.transData.transform((0.5, 50.0 + k * 5.0))[1]
            assert py_l == pytest.approx(py_h, abs=1e-6), f"k={k}"

        # 3. 离群点钉边：图上有 ↑1000.000 标注文本
        texts = [t.get_text() for t in ax_l.texts]
        assert any("↑" in t and "1000.000" in t for t in texts), texts


# ============================================================================
# 回归：单水平图样式保持 spec §E.1 不变
# ============================================================================

class TestSingleChartStylesUnchanged:
    def test_single_chart_keeps_spec_e1_styles(self, conn):
        """单水平图：在控黑圆点 / 警告黄三角 / 失控红实心圆（§E.1 不动）。"""
        ids = _seed_pair(conn)
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-01",
            value=10.2, level="L", status="在控")
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-02",
            value=12.2, level="L", status="警告(1_2s)",
            rules='["1_2s"]')
        _insert_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"], date="2026-05-03",
            value=13.5, level="L", status="失控(1_3s)",
            rules='["1_3s"]')

        fig = LJ.render_lj_chart(
            _chart_data(conn, ids, level="L"),
            meta=LJ.make_default_meta(),
        )
        ax = fig.axes[0]
        assert _point_styles(ax) == {
            ("o", "#000000"): 1, ("^", YELLOW): 1, ("o", RED): 1,
        }
