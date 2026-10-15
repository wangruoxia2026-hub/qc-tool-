"""
PR#2 — 用户需求 2026-10-14：桌面质控图点击数据点 → 图表右侧展示排查面板
（在控：日期/序号/检测值/状态；警告：+触发规则；失控：+触发规则+spec §F.1 排查清单，
仅只读展示，不勾选不保存——2026-10-14 用户拍板）。

Run:
    python -m pytest tests/test_lj_chart_click_panel.py -v
"""
from __future__ import annotations

import os

# Force offscreen Qt platform before any PyQt6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import types
from pathlib import Path

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool import lj_chart as LJ


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


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
    p = P.create_project(conn, name="Vitamin D", short="Vit-D",
                         unit="ng/mL", manufacturer="Roche")
    ti_l = P.create_test_item(conn, p.id, name="D2", lot="LOT1", level="L",
                              target=100.0, sd=2.0, short="D2-S", unit="ng/mL",
                              manufacturer="Roche", operator="Op-1",
                              pre_processor="Pre-1")
    P.create_test_item(conn, p.id, name="D2", lot="LOT1", level="H",
                       target=100.0, sd=2.0, short="D2-S", unit="ng/mL",
                       manufacturer="Roche", operator="Op-1",
                       pre_processor="Pre-1")
    M.create_measurement(conn, test_item_id=ti_l.id, project_id=p.id,
                         lot="LOT1", level="L", date="2026-04-01", value=100.5)
    conn.commit()
    return {"project_name": p.name, "test_item_id_l": ti_l.id,
            "test_item_name": "D2", "lot": "LOT1"}


def _point(*, status: str, rules: list[str], value: float = 100.0) -> LJ.LJChartPoint:
    return LJ.LJChartPoint(
        date="2026-04-01", seq=1, value=value, sd_score=(value - 100.0) / 2.0,
        status=LJ._ChartStatus(status), rules=rules, level="L", lot="LOT1",
        measurement_id=999,
    )


def _fake_event(ax, xdata, ydata):
    return types.SimpleNamespace(inaxes=ax, xdata=xdata, ydata=ydata)


# ============================================================================
# LJChartDialog 右侧排查面板
# ============================================================================

class TestLJChartDialogPanel:
    def test_dialog_has_right_invest_panel(self, qapp, conn):
        """图表对话框右侧存在 320px 排查面板（只读 QTextBrowser）。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        _seed(conn)
        dlg = LJChartDialog(conn)
        dlg.show()
        assert dlg.invest_panel is not None
        assert dlg.invest_panel.width() == 320
        assert "点击图上数据点查看排查信息" in dlg.invest_panel.toHtml()

    def test_ooc_point_no_saved_shows_hint_and_edit(self, qapp, conn):
        """失控点（未保存过排查项）：点信息 + 提示 + 编辑入口，不展示全量清单。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        _seed(conn)
        dlg = LJChartDialog(conn)
        dlg._on_point_clicked(_point(status="out_of_control", rules=["1_3s"],
                                     value=107.0))
        html = dlg.invest_panel.toHtml()
        assert "数据点排查信息" in html
        assert "2026-04-01" in html and "序号" in html and "107.000" in html
        assert "失控" in html
        assert "触发规则" in html and "1_3s" in html
        assert "排查清单（spec §F.1）" in html
        assert "尚未记录排查项" in html
        assert "编辑排查清单" in html
        assert "试剂：是否新开瓶 / 是否过期 / 是否污染" not in html, \
            "未保存时不应展示全量清单"

    def test_warning_point_renders_rule_without_checklist(self, qapp, conn):
        """警告点：状态警告 + 触发规则（1_2s），无排查清单。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        _seed(conn)
        dlg = LJChartDialog(conn)
        dlg._on_point_clicked(_point(status="warning", rules=["1_2s"], value=104.5))
        html = dlg.invest_panel.toHtml()
        assert "警告" in html
        assert "触发规则" in html and "1_2s" in html
        assert "排查清单" not in html

    def test_in_control_point_renders_basic_info_only(self, qapp, conn):
        """在控点：日期/序号/检测值/状态在控，无规则行、无清单。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        _seed(conn)
        dlg = LJChartDialog(conn)
        dlg._on_point_clicked(_point(status="in_control", rules=[], value=100.5))
        html = dlg.invest_panel.toHtml()
        assert "在控" in html
        assert "100.500" in html
        assert "触发规则" not in html
        assert "排查清单" not in html


# ============================================================================
# attach_click_panel：button_press_event → 最近点 → on_pick
# ============================================================================

class TestAttachClickPanel:
    def _make_canvas(self, data: LJ.LJChartData):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        fig = Figure()
        ax = fig.add_subplot(111)
        ax.plot([float(i) for i in range(1, len(data.points) + 1)],
                [p.value for p in data.points])
        canvas = FigureCanvasQTAgg(fig)
        return fig, ax, canvas

    def _data(self, values):
        points = [LJ.LJChartPoint(
            date=f"2026-04-0{i}", seq=1, value=v, sd_score=0.0,
            status=LJ._ChartStatus.IN_CONTROL, rules=[], level="L", lot="LOT1",
            measurement_id=i,
        ) for i, v in enumerate(values, start=1)]
        return LJ.LJChartData(
            points=points, target=100.0, sd=2.0, project_name="P",
            project_short=None, test_item_name="D2", test_item_short=None,
            unit=None, manufacturer=None, lot="LOT1", level="L",
            operator=None, pre_processor=None, year=2026, month=4,
            date_range=("2026-04-01", "2026-04-30"),
        )

    def test_click_near_point_triggers_on_pick(self, qapp):
        """点击落在某点附近（x/y 容差内）→ on_pick 收到该点。"""
        from qc_tool.ui_lj_chart import attach_click_panel
        data = self._data([100.0, 102.0, 101.0])
        fig, ax, canvas = self._make_canvas(data)
        picked = []
        attach_click_panel(fig, canvas, data, picked.append)
        # 点击第 2 个点附近（x≈2.0, y≈102.0）
        canvas._click_handler(_fake_event(ax, 2.05, 102.2))
        assert len(picked) == 1
        assert picked[0].value == 102.0
        assert picked[0].measurement_id == 2

    def test_click_far_from_points_ignored(self, qapp):
        """点击距最近点 x 超出容差 → 不触发 on_pick。"""
        from qc_tool.ui_lj_chart import attach_click_panel
        data = self._data([100.0, 102.0, 101.0])
        fig, ax, canvas = self._make_canvas(data)
        picked = []
        attach_click_panel(fig, canvas, data, picked.append)
        canvas._click_handler(_fake_event(ax, 50.0, 101.0))
        assert picked == []

    def test_click_outside_axes_ignored(self, qapp):
        """点击不在任何 axes 内 → 不触发。"""
        from qc_tool.ui_lj_chart import attach_click_panel
        data = self._data([100.0])
        fig, _ax, canvas = self._make_canvas(data)
        picked = []
        attach_click_panel(fig, canvas, data, picked.append)
        canvas._click_handler(_fake_event(None, 1.0, 100.0))
        assert picked == []

    def test_no_points_detaches_listener(self, qapp):
        """无数据点 → 不挂监听、不崩溃。"""
        from qc_tool.ui_lj_chart import attach_click_panel
        data = self._data([])
        fig, _ax, canvas = self._make_canvas(data)
        attach_click_panel(fig, canvas, data, lambda p: None)
        assert getattr(canvas, "_click_handler", None) is None


# ============================================================================
# 排查面板联动 F.1：编辑保存 → 点击只展示已勾选（2026-10-14 用户补充裁定）
# ============================================================================

class TestOocPanelEditLinkF1:
    def _seed_ooc(self, conn):
        """建一个失控点并写入已保存的排查记录（模拟旧弹窗保存过）。"""
        from qc_tool import investigation as I
        p = P.create_project(conn, name="Vitamin D", short="Vit-D",
                             unit="ng/mL", manufacturer="Roche")
        ti = P.create_test_item(conn, p.id, name="D2", lot="LOT1", level="L",
                                target=100.0, sd=2.0)
        m = M.create_measurement(conn, test_item_id=ti.id, project_id=p.id,
                                 lot="LOT1", level="L", date="2026-04-02",
                                 value=107.0)  # +3.5SD → 失控
        conn.commit()
        # 确保存在 out_of_control 行（判定自动插入；此处兜底插入）
        if I.get_ooc_checklist(conn, m.id) is None:
            conn.execute(
                "INSERT INTO out_of_control(measurement_id, test_item_id, project_id,"
                " lot, level, triggered_date, test_item_name, project_name, rules,"
                " error_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (m.id, ti.id, p.id, "LOT1", "L", "2026-04-02", "D2", "Vitamin D",
                 '["1_2s","1_3s"]', "随机"),
            )
            conn.commit()
        I.save_investigation(
            conn, measurement_id=m.id, rules=["1_3s"],
            checklist={"随机误差嫌疑": ["试剂：是否新开瓶 / 是否过期 / 是否污染"]},
            conclusion="已换新开瓶试剂",
        )
        conn.commit()
        return m.id

    def test_ooc_view_shows_only_saved_items(self, qapp, conn):
        """失控点查看模式：只展示已勾选保存的项，未勾选的不显示。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        mid = self._seed_ooc(conn)
        dlg = LJChartDialog(conn)
        # _point 的 measurement_id 是 999；换成真实 mid 再渲染
        p = _point(status="out_of_control", rules=["1_3s"], value=107.0)
        from dataclasses import replace as _replace
        dlg._on_point_clicked(_replace(p, measurement_id=mid))
        html = dlg.invest_panel.toHtml()
        assert "试剂：是否新开瓶 / 是否过期 / 是否污染" in html
        assert "仪器：泵阀、管道气泡、光路、电压" not in html, "未勾选项不应展示"
        assert "处理结论" in html and "已换新开瓶试剂" in html
        assert "编辑排查清单" in html

    def test_2_2s_saved_items_show_both_categories(self, qapp, conn):
        """2_2s 已保存的排查项 → 查看模式同时展示随机+系统两个分类（issue #6 决议）。"""
        from qc_tool import investigation as I
        from qc_tool.ui_lj_chart import LJChartDialog
        mid = self._seed_ooc(conn)
        I.save_investigation(
            conn, measurement_id=mid, rules=["2_2s"],
            checklist={
                "随机误差嫌疑": ["试剂：是否新开瓶 / 是否过期 / 是否污染"],
                "系统误差嫌疑": ["校准：是否近期校准 / 标准曲线是否漂移"],
            },
        )
        conn.commit()
        dlg = LJChartDialog(conn)
        p = _point(status="out_of_control", rules=["2_2s"], value=107.0)
        from dataclasses import replace as _replace
        dlg._on_point_clicked(_replace(p, measurement_id=mid))
        html = dlg.invest_panel.toHtml()
        assert "随机误差嫌疑" in html
        assert "系统误差嫌疑" in html
        assert "试剂：是否新开瓶 / 是否过期 / 是否污染" in html
        assert "校准：是否近期校准 / 标准曲线是否漂移" in html

    def test_ooc_view_no_saved_shows_hint(self, qapp, conn):
        """未保存过排查项：查看模式显示提示 + 编辑入口。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        _seed(conn)
        dlg = LJChartDialog(conn)
        dlg._on_point_clicked(_point(status="out_of_control", rules=["1_3s"],
                                     value=107.0))
        html = dlg.invest_panel.toHtml()
        assert "尚未记录排查项" in html
        assert "编辑排查清单" in html
        assert "试剂：是否新开瓶 / 是否过期 / 是否污染" not in html

    def test_investigation_dialog_prefill_web_format(self, qapp, conn):
        """编辑入口复用旧弹窗并预选已保存项（web 落库格式 {cat:[items]}）。"""
        from qc_tool.main_window_b import InvestigationDialog
        from qc_tool import investigation as I
        plan = I.build_investigation_plan(
            rules=["1_3s"], triggered_date="2026-04-01", project_name="P",
            test_item_name="D2", lot="LOT1", level="L", value=107.0)
        prefill = {
            "checklist": {"随机误差嫌疑": ["试剂：是否新开瓶 / 是否过期 / 是否污染"]},
            "conclusion": "已处理",
        }
        dlg = InvestigationDialog(None, plan=plan, measurement_id=1, prefill=prefill)
        checked = [k for k, cb in dlg._checkboxes.items() if cb.isChecked()]
        assert checked == ["随机误差嫌疑_试剂：是否新开瓶 / 是否过期 / 是否污染"]
        assert dlg.conclusion_edit.text() == "已处理"

    def test_investigation_dialog_prefill_bool_format(self, qapp, conn):
        """兼容桌面旧弹窗落库格式 {"分类_条目": bool}。"""
        from qc_tool.main_window_b import InvestigationDialog
        from qc_tool import investigation as I
        plan = I.build_investigation_plan(
            rules=["4_1s"], triggered_date="2026-04-01", project_name="P",
            test_item_name="D2", lot="LOT1", level="L", value=104.0)
        prefill = {"checklist": {"系统误差嫌疑_环境：温度、湿度": True,
                                 "系统误差嫌疑_校准：是否近期校准 / 标准曲线是否漂移": False}}
        dlg = InvestigationDialog(None, plan=plan, measurement_id=1, prefill=prefill)
        checked = [k for k, cb in dlg._checkboxes.items() if cb.isChecked()]
        assert checked == ["系统误差嫌疑_环境：温度、湿度"]

    def test_checklist_to_categories_both_formats(self):
        """两种落库格式归一：list 格式与 bool 格式；空/未勾选 → 空。"""
        from qc_tool import investigation as I
        assert I.checklist_to_categories({"随机误差嫌疑": ["a", "b"]}) == \
            {"随机误差嫌疑": ["a", "b"]}
        assert I.checklist_to_categories(
            {"随机误差嫌疑_x": True, "随机误差嫌疑_y": False, "其他_z": True}) == \
            {"随机误差嫌疑": ["x"], "其他": ["z"]}
        assert I.checklist_to_categories(None) == {}
        assert I.checklist_to_categories({"随机误差嫌疑": []}) == {}
        assert I.checklist_to_categories("not-a-dict") == {}


# ============================================================================
# 结果发放建议（用户需求 2026-10-14：排查信息下方，仅失控点显示）
# ============================================================================

class TestReleaseAdvicePanel:
    def _seed_day(self, conn, *, ooc_seqs: list[int]) -> dict:
        """项目 + D2 指标 L/H + L 同日 1-5 号（ooc_seqs 给 115.0 失控）→ 返回 {seq: mid}。"""
        p = P.create_project(conn, name="Vitamin D", short="Vit-D",
                             unit="ng/mL", manufacturer="Roche")
        ti_l = P.create_test_item(conn, p.id, name="D2", lot="LOT-L", level="L",
                                  target=100.0, sd=2.0)
        P.create_test_item(conn, p.id, name="D2", lot="LOT-H", level="H",
                           target=50.0, sd=1.0)
        mids = {}
        for seq in range(1, 6):
            v = 115.0 if seq in ooc_seqs else 100.0
            m = M.create_measurement(conn, test_item_id=ti_l.id, project_id=p.id,
                                     lot="LOT-L", level="L", date="2026-04-01",
                                     value=v, seq=seq)
            mids[seq] = m.id
        conn.commit()
        return mids

    def test_ooc_middle_point_shows_advice(self, qapp, conn):
        """中间失控点：排查信息下方显示 2-4 号区间建议。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        mid = self._seed_day(conn, ooc_seqs=[3])[3]
        dlg = LJChartDialog(conn)
        p = _point(status="out_of_control", rules=["1_3s"], value=115.0)
        from dataclasses import replace as _replace
        dlg._on_point_clicked(_replace(p, measurement_id=mid))
        html = dlg.invest_panel.toHtml()
        assert "结果发放建议" in html
        assert "2-4 号质控点区间内的样本结果不允许发放" in html

    def test_ooc_last_point_shows_prev_to_self(self, qapp, conn):
        """当日最后失控点：显示 4-5 号区间建议。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        mid = self._seed_day(conn, ooc_seqs=[5])[5]
        dlg = LJChartDialog(conn)
        p = _point(status="out_of_control", rules=["1_3s"], value=115.0)
        from dataclasses import replace as _replace
        dlg._on_point_clicked(_replace(p, measurement_id=mid))
        html = dlg.invest_panel.toHtml()
        assert "结果发放建议" in html
        assert "4-5 号质控点区间内的样本结果不允许发放" in html

    def test_ooc_consecutive_points_show_merged_advice(self, qapp, conn):
        """3、4 号连续失控：点 3 / 点 4 都显示合并区间 当日2-5。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        mids = self._seed_day(conn, ooc_seqs=[3, 4])
        dlg = LJChartDialog(conn)
        p = _point(status="out_of_control", rules=["1_3s"], value=115.0)
        from dataclasses import replace as _replace
        for seq in (3, 4):
            dlg._on_point_clicked(_replace(p, measurement_id=mids[seq]))
            html = dlg.invest_panel.toHtml()
            assert "结果发放建议" in html
            assert "当日2-5质控点区间内结果不允许发放" in html, f"序号 {seq} 应显示合并区间"

    def test_in_control_point_no_advice(self, qapp, conn):
        """在控点：不出现结果发放建议区块。"""
        from qc_tool.ui_lj_chart import LJChartDialog
        _seed(conn)
        dlg = LJChartDialog(conn)
        dlg._on_point_clicked(_point(status="in_control", rules=[], value=100.5))
        html = dlg.invest_panel.toHtml()
        assert "结果发放建议" not in html
