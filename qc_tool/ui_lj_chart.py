"""
Functional E UI: Levey-Jennings QC Chart dialog (spec V1.3 §E.1-E.2 + §10).

Public widgets
--------------
- ChartConfigDialog : 6 QCheckBox fields（新标题格式，用户拍板 2026-10-13）
- LJChartDialog     : main window with 5 dropdowns + canvas + controls
- attach_hover_tooltip(fig, canvas, data) : hover -> QToolTip

Dependencies:
- PyQt6 (already in repo)
- matplotlib (already in repo) — Agg backend; UI uses FigureCanvasQTAgg

Design:
1. Only reads from measurements + projects + lab_settings; never writes.
2. No navigation toolbar (matplotlib 3.11 changed the import path;
   we provide Prev/Next month buttons in pure Qt instead).
3. Hover tooltip uses matplotlib's mpl_connect('motion_notify_event') and
   QToolTip.showText; no mplcursors dependency.
4. Configurable 6 title fields all default to visible; date_range is
   always-on fallback (lj_chart.make_default_meta enforces this).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Optional

from PyQt6.QtCore import Qt as QtCoreEnum, pyqtSignal as _unused_signal  # noqa: F401
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QToolTip,
)

import matplotlib
matplotlib.use("Agg")  # safe default
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from . import projects as P
from . import lj_chart as LJ
from . import investigation as I
from . import release_advice as RA


# ============================================================================
# Chart config dialog（新标题格式 6 字段，用户拍板 2026-10-13）
# ============================================================================

# Display labels for each field key (Chinese)
_FIELD_LABELS: dict[str, str] = {
    "project_name":  "项目名称",
    "lot":           "质控品批号",
    "target":        "靶值",
    "sd":            "SD",
    "operator":      "仪器操作",
    "pre_processor": "前处理",
}


class ChartConfigDialog(QDialog):
    """6-checkBox dialog for chart title visibility.

    新标题格式只渲染 TITLE_FIELDS 6 项（test_item_name/short/unit/
    manufacturer/level 不再上标题）；时间范围恒显示。
    """

    def __init__(self, parent: Optional[QWidget] = None, *,
                 meta: LJ.ChartMeta) -> None:
        super().__init__(parent)
        self.setWindowTitle("图表配置")
        self._meta = meta
        self._checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "勾选要在质控图标题区显示的项目（时间范围始终显示）。"
        ))

        for key in LJ.TITLE_FIELDS:
            cb = QCheckBox(_FIELD_LABELS.get(key, key))
            cb.setChecked(key in meta.visible_fields)
            self._checkboxes[key] = cb
            layout.addWidget(cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_checked(self, key: str, checked: bool) -> None:
        if key in self._checkboxes:
            self._checkboxes[key].setChecked(checked)

    @property
    def meta(self) -> LJ.ChartMeta:
        """Return current visibility as a fresh ChartMeta."""
        fields = {
            key for key, cb in self._checkboxes.items() if cb.isChecked()
        }
        fields.add("date_range")  # always-on fallback
        return LJ.ChartMeta(visible_fields=fields)


# ============================================================================
# Main chart dialog
# ============================================================================

class LJChartDialog(QDialog):
    """Levey-Jennings chart dialog (spec §E).

    Layout:
        Row 1 (filters): Project / Test Item / Level / Lot / Year-Month
        Row 2 (nav): Prev month / Month combo / Next month / Chart config / Export / Close
        Body:  matplotlib FigureCanvasQTAgg with embedded LJ chart
    """

    def __init__(self, conn: sqlite3.Connection, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Levey-Jennings Chart")
        self.resize(1380, 720)

        # State
        self._current_data: Optional[LJ.LJChartData] = None
        # 用户需求（2026-10-12 第二次补充）：L+H 双轴单图模式下保存 (data_l, data_h)
        self._current_data_pair: Optional[tuple[LJ.LJChartData, LJ.LJChartData]] = None
        self._current_figure: Optional[Figure] = None

        # ---- Top: filter dropdowns ----
        self.project_combo = QComboBox()
        self.test_item_combo = QComboBox()
        self.level_combo = QComboBox()
        # 默认 L+H 双轴（一图双 y 轴：左轴 L、右轴 H，控制限重合；
        # 用户需求 2026-10-12 第二次补充，spec §10.14 经用户拍板开放）
        self.level_combo.addItem("L+H 双轴", userData="BOTH")
        self.level_combo.addItem("L", userData="L")
        self.level_combo.addItem("H", userData="H")
        self.lot_combo = QComboBox()
        self.year_month_combo = QComboBox()

        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        self.test_item_combo.currentIndexChanged.connect(self._on_test_item_changed)
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)
        self.lot_combo.currentIndexChanged.connect(self._on_lot_changed)
        self.year_month_combo.currentIndexChanged.connect(self._on_year_month_changed)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Project:"))
        filter_row.addWidget(self.project_combo)
        filter_row.addWidget(QLabel("Test Item:"))
        filter_row.addWidget(self.test_item_combo)
        filter_row.addWidget(QLabel("Level:"))
        filter_row.addWidget(self.level_combo)
        filter_row.addWidget(QLabel("Lot:"))
        filter_row.addWidget(self.lot_combo)
        filter_row.addWidget(QLabel("Month:"))
        filter_row.addWidget(self.year_month_combo)

        # ---- Middle: nav buttons ----
        self.btn_prev = QPushButton("< Prev Month")
        self.btn_next = QPushButton("Next Month >")
        self.btn_config = QPushButton("Chart Configuration")
        self.btn_export = QPushButton("Export Image")
        self.btn_close = QPushButton("Close")

        self.btn_prev.clicked.connect(self._on_prev_month)
        self.btn_next.clicked.connect(self._on_next_month)
        self.btn_config.clicked.connect(self._on_config)
        self.btn_export.clicked.connect(self._on_export)
        self.btn_close.clicked.connect(self.reject)

        nav_row = QHBoxLayout()
        nav_row.addWidget(self.btn_prev)
        nav_row.addWidget(self.btn_next)
        nav_row.addStretch()
        nav_row.addWidget(self.btn_config)
        nav_row.addWidget(self.btn_export)
        nav_row.addWidget(self.btn_close)

        # ---- Body: matplotlib canvas ----
        self.placeholder_figure = Figure(figsize=(11, 6))
        self.placeholder_figure.text(
            0.5, 0.5, "No Data", ha="center", va="center",
            fontsize=20, color="#888888",
        )
        self.canvas = FigureCanvasQTAgg(self.placeholder_figure)
        self.canvas.setParent(self)

        # 右侧排查面板（用户需求 2026-10-14：点击图上点 → 右侧展示排查信息）
        self.invest_panel = QTextBrowser()
        self.invest_panel.setFixedWidth(320)
        self.invest_panel.setHtml(
            '<p style="color:#94a3b8;">点击图上数据点查看排查信息</p>'
        )
        self.invest_panel.setOpenLinks(False)  # 链接只发信号，不触发导航
        self.invest_panel.anchorClicked.connect(self._on_panel_edit)
        self._last_clicked_point: Optional[LJ.LJChartPoint] = None

        chart_column = QVBoxLayout()
        chart_column.addLayout(filter_row)
        chart_column.addLayout(nav_row)
        chart_column.addWidget(self.canvas)

        body = QHBoxLayout()
        body.addLayout(chart_column, 1)
        body.addWidget(self.invest_panel)

        self.setLayout(body)

        # Populate
        self._populate_projects()

    # ------------------------------------------------------------------
    # Population / cascading dropdowns
    # ------------------------------------------------------------------

    def _populate_projects(self) -> None:
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for p in P.list_projects(self.conn):
            self.project_combo.addItem(p.name, userData=p.id)
        self.project_combo.blockSignals(False)
        if self.project_combo.count() > 0:
            self.project_combo.setCurrentIndex(0)
            self._on_project_changed(0)

    def _on_project_changed(self, _idx: int) -> None:
        pid = self.project_combo.currentData()
        if pid is None:
            self.test_item_combo.clear()
            return
        self._populate_test_items(pid)

    def _populate_test_items(self, project_id: int) -> None:
        self.test_item_combo.blockSignals(True)
        self.test_item_combo.clear()
        for ti in P.list_test_items(self.conn, project_id):
            label = f"{ti.name} (lot={ti.lot}, level={ti.level})"
            self.test_item_combo.addItem(
                label, userData=(ti.name, ti.lot, ti.level),
            )
        self.test_item_combo.blockSignals(False)
        if self.test_item_combo.count() > 0:
            self.test_item_combo.setCurrentIndex(0)
            self._on_test_item_changed(0)

    def _on_test_item_changed(self, _idx: int) -> None:
        data = self.test_item_combo.currentData()
        if data is None:
            self.lot_combo.clear()
            return
        # 不再强制把 Level 下拉同步成该指标的单一水平：
        # 默认停在 "L+H 双轴"，由 _refresh 负责缺水平时回退单图。
        self._populate_lots_for(data[0])

    def _populate_lots_for(self, test_item_name: str) -> None:
        pid = self.project_combo.currentData()
        if pid is None:
            return
        self.lot_combo.blockSignals(True)
        self.lot_combo.clear()
        lots = P.list_lots(self.conn, pid, test_item_name)
        for lot in lots:
            self.lot_combo.addItem(lot, userData=lot)
        self.lot_combo.blockSignals(False)
        if self.lot_combo.count() > 0:
            self.lot_combo.setCurrentIndex(0)
            self._on_lot_changed(0)

    def _on_level_changed(self, _idx: int) -> None:
        # Re-filter lots to those that have data on this level.
        self._populate_lots_for(self._current_test_item_name())
        self._refresh()

    def _current_level(self) -> str:
        """Level 下拉的逻辑值：'BOTH' / 'L' / 'H'（优先 userData，回退文本）。"""
        data = self.level_combo.currentData()
        if data:
            return str(data)
        return self.level_combo.currentText()

    def _on_lot_changed(self, _idx: int) -> None:
        self._populate_months()

    def _populate_months(self) -> None:
        self.year_month_combo.blockSignals(True)
        self.year_month_combo.clear()
        months = self._filter_months(
            project_id=self.project_combo.currentData() or 0,
            test_item_name=self._current_test_item_name(),
            lot=self.lot_combo.currentData() or "",
            level=self._current_level(),
        )
        for (y, m) in months:
            label = f"{y:04d}-{m:02d}"
            self.year_month_combo.addItem(label, userData=(y, m))
        self.year_month_combo.blockSignals(False)
        if self.year_month_combo.count() > 0:
            self.year_month_combo.setCurrentIndex(0)
            self._refresh()

    def _on_year_month_changed(self, _idx: int) -> None:
        self._refresh()

    def _current_test_item_name(self) -> str:
        data = self.test_item_combo.currentData()
        return data[0] if data else ""

    # ------------------------------------------------------------------
    # Month filter (used by tests + populate_months)
    # ------------------------------------------------------------------

    def _filter_months(
        self,
        *,
        project_id: int,
        test_item_name: str,
        lot: str,
        level: str,
    ) -> list[tuple[int, int]]:
        """Return [(year, month), ...] for measurements matching the filter.

        spec §E.1（2026-10-12 用户确认）：作废点不显示（compute_chart_data
        数据层排除）。月份列表仍按原始测量行统计，纯作废月份图上显示"暂无数据"。

        level='BOTH'（L+H 双轴）时返回两水平月份的并集。
        """
        if level == "BOTH":
            rows = self.conn.execute(
                "SELECT DISTINCT substr(m.date, 1, 4) AS y, "
                "                substr(m.date, 6, 2) AS mo "
                "FROM measurements m "
                "JOIN test_items t ON t.id = m.test_item_id "
                "WHERE m.project_id = ? AND t.name = ? "
                "  AND m.lot = ? AND m.level IN ('L', 'H') "
                "ORDER BY y, mo",
                (project_id, test_item_name, lot),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT substr(m.date, 1, 4) AS y, "
                "                substr(m.date, 6, 2) AS mo "
                "FROM measurements m "
                "JOIN test_items t ON t.id = m.test_item_id "
                "WHERE m.project_id = ? AND t.name = ? "
                "  AND m.lot = ? AND m.level = ? "
                "ORDER BY y, mo",
                (project_id, test_item_name, lot, level),
            ).fetchall()
        out: list[tuple[int, int]] = []
        for r in rows:
            try:
                out.append((int(r["y"]), int(r["mo"])))
            except (TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------
    # Build ChartMeta from current dialog state
    # ------------------------------------------------------------------

    def _build_meta(self) -> LJ.ChartMeta:
        """Build ChartMeta from current ChartConfigDialog state (or defaults)."""
        return LJ.make_default_meta()

    # ------------------------------------------------------------------
    # Refresh / render
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        pid = self.project_combo.currentData()
        ti_name = self._current_test_item_name()
        lot = self.lot_combo.currentData()
        level = self._current_level()
        ym = self.year_month_combo.currentData()
        if pid is None or not ti_name or not lot or ym is None:
            self._show_no_data()
            return
        year, month = ym

        # ---- L+H 双轴模式（用户需求 2026-10-12 第二次补充）----
        if level == "BOTH":
            items = P.list_test_items(self.conn, pid, name=ti_name)
            item_l = next(
                (x for x in items if x.lot == lot and x.level == "L"), None)
            item_h = next(
                (x for x in items if x.lot == lot and x.level == "H"), None)
            if item_l is not None and item_h is not None:
                data_l = LJ.compute_chart_data(
                    self.conn, project_id=pid, test_item_name=ti_name,
                    lot=lot, level="L", year=year, month=month,
                )
                data_h = LJ.compute_chart_data(
                    self.conn, project_id=pid, test_item_name=ti_name,
                    lot=lot, level="H", year=year, month=month,
                )
                if not data_l.points and not data_h.points:
                    self._show_no_data()
                    return
                meta = self._build_meta()
                fig = LJ.render_lj_chart_dual_axis(data_l, data_h, meta=meta)
                self._current_data = data_l
                self._current_data_pair = (data_l, data_h)
                self._current_figure = fig
                self._swap_canvas(fig)
                attach_hover_tooltip(fig, self.canvas, [data_l, data_h])
                attach_click_panel(fig, self.canvas, [data_l, data_h],
                                   self._on_point_clicked)
                return
            # 同批号缺一水平 -> 回退到存在的那一水平单图
            fallback = item_l or item_h
            level = fallback.level if fallback is not None else "L"

        # ---- 单水平模式（原有逻辑不变）----
        data = LJ.compute_chart_data(
            self.conn, project_id=pid,
            test_item_name=ti_name, lot=lot, level=level,
            year=year, month=month,
        )
        self._current_data = data
        self._current_data_pair = None
        if not data.points:
            self._show_no_data()
            return
        meta = self._build_meta()
        fig = LJ.render_lj_chart(data, meta=meta)
        self._current_figure = fig
        self._swap_canvas(fig)
        attach_hover_tooltip(fig, self.canvas, data)
        attach_click_panel(fig, self.canvas, data, self._on_point_clicked)

    def _show_no_data(self) -> None:
        # Render a placeholder figure (always 'No Data' message)
        fig = LJ.render_lj_chart(
            LJ.LJChartData(
                points=[], target=0.0, sd=0.0,
                project_name="", project_short=None,
                test_item_name="", test_item_short=None,
                unit=None, manufacturer=None,
                lot="", level="L",
                operator=None, pre_processor=None,
                year=0, month=0,
                date_range=("", ""),
            ),
            meta=LJ.make_default_meta(),
        )
        self._current_data = None
        self._current_data_pair = None
        self._current_figure = fig
        self._swap_canvas(fig)

    def _swap_canvas(self, fig: Figure) -> None:
        old = self.canvas
        self.canvas = FigureCanvasQTAgg(fig)
        self.canvas.setParent(self)
        # Replace old canvas in body layout
        layout = self.layout()
        if layout is not None:
            layout.replaceWidget(old, self.canvas)
        old.setParent(None)
        old.deleteLater()
        self.canvas.draw()

    # ------------------------------------------------------------------
    # 点点击 → 右侧排查面板（用户需求 2026-10-14；spec §F.1 清单仅展示）
    # ------------------------------------------------------------------

    def _on_point_clicked(self, p: LJ.LJChartPoint) -> None:
        """渲染右侧排查面板：在控/警告显示日期、序号、值、状态（+规则）；
        失控 → 查看模式（只展示已勾选结果）+「编辑排查清单」入口，联动 F.1。"""
        self._last_clicked_point = p
        label = _POINT_STATUS_LABEL.get(
            p.status.value if hasattr(p.status, "value") else str(p.status),
            str(p.status),
        )
        if label == "失控":
            self._render_ooc_view(p)
        else:
            self.invest_panel.setHtml(self._point_info_html(p))

    def _point_info_html(self, p: LJ.LJChartPoint) -> str:
        label = _POINT_STATUS_LABEL.get(
            p.status.value if hasattr(p.status, "value") else str(p.status),
            str(p.status),
        )
        color = _POINT_STATUS_COLOR.get(
            p.status.value if hasattr(p.status, "value") else str(p.status),
            "#334155",
        )
        lines = [
            '<h3 style="margin:0 0 4px 0;">数据点排查信息</h3>',
            '<table cellspacing="0" cellpadding="2">',
            f"<tr><td style='color:#64748b;'>检测日期</td><td>{p.date}</td></tr>",
            f"<tr><td style='color:#64748b;'>序号</td><td>{p.seq}</td></tr>",
            f"<tr><td style='color:#64748b;'>检测值</td><td>{p.value:.3f}</td></tr>",
            f"<tr><td style='color:#64748b;'>水平</td><td>{p.level}</td></tr>",
            f"<tr><td style='color:#64748b;'>状态</td>"
            f"<td style='color:{color}; font-weight:bold;'>{label}</td></tr>",
        ]
        rules_str = ",".join(p.rules)
        if rules_str:
            lines.append(
                f"<tr><td style='color:#64748b;'>触发规则</td>"
                f"<td style='color:#c0392b; font-weight:bold;'>{rules_str}</td></tr>"
            )
        lines.append("</table>")
        return "".join(lines)

    def _render_ooc_view(self, p: LJ.LJChartPoint) -> None:
        """失控点查看模式：只展示已勾选保存的排查项（联动 F.1 checklist）+ 编辑入口。"""
        rec = I.get_ooc_checklist(self.conn, p.measurement_id)
        saved = I.checklist_to_categories(rec["checklist"]) if rec else {}
        shown = {c: items for c, items in saved.items() if items}
        lines = [
            self._point_info_html(p),
        ]
        # 结果发放建议（用户需求 2026-10-14）：仅失控点显示，区间只在同日闭合
        advice = RA.build_release_advice(self.conn, p.measurement_id)
        if advice:
            lines.append(
                '<h4 style="color:#c0392b; margin:10px 0 4px 0;">结果发放建议</h4>'
                f'<p style="color:#c0392b; font-weight:bold; margin:0;">'
                f'{advice["text"]}</p>'
            )
        lines.append(
            '<h4 style="color:#c0392b; margin:10px 0 4px 0;">'
            '排查清单（spec §F.1）</h4>',
        )
        if shown:
            for cat, items in shown.items():
                lines.append(
                    f"<p style='font-weight:bold; margin:6px 0 2px 0;'>{cat}</p>"
                    "<ul style='margin:0 0 0 18px;'>"
                )
                for item in items:
                    lines.append(f"<li>{item}</li>")
                lines.append("</ul>")
            if rec and rec.get("conclusion"):
                lines.append(
                    f"<p style='margin-top:8px; color:#475569;'>"
                    f"<b>处理结论：</b>{rec['conclusion']}</p>"
                )
        else:
            lines.append('<p style="color:#94a3b8;">尚未记录排查项</p>')
        lines.append(
            '<p style="margin-top:8px;"><a href="#edit" style="color:#0284c7;">'
            '编辑排查清单</a></p>'
        )
        self.invest_panel.setHtml("".join(lines))

    def _on_panel_edit(self, url: Any) -> None:
        """「编辑排查清单」→ 复用主窗口的 §F.1 InvestigationDialog（预选已保存项），
        保存后回查看模式——与旧弹窗同一 save_investigation 路径，数据互通。"""
        if url.toString() != "#edit":
            return
        p = getattr(self, "_last_clicked_point", None)
        if p is None:
            return
        from .main_window_b import InvestigationDialog  # 懒导入防循环
        data = self._current_data
        plan = I.build_investigation_plan(
            rules=p.rules,
            triggered_date=p.date,
            project_name=data.project_name if data is not None else "?",
            test_item_name=data.test_item_name if data is not None else "?",
            lot=p.lot,
            level=p.level,
            value=p.value,
        )
        rec = I.get_ooc_checklist(self.conn, p.measurement_id)
        dlg = InvestigationDialog(
            self, plan=plan, measurement_id=p.measurement_id, prefill=rec,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._render_ooc_view(p)

    # ------------------------------------------------------------------
    # Nav / config / export buttons
    # ------------------------------------------------------------------

    def _on_prev_month(self) -> None:
        if self.year_month_combo.count() == 0:
            return
        i = max(0, self.year_month_combo.currentIndex() - 1)
        self.year_month_combo.setCurrentIndex(i)

    def _on_next_month(self) -> None:
        if self.year_month_combo.count() == 0:
            return
        i = min(self.year_month_combo.count() - 1,
                self.year_month_combo.currentIndex() + 1)
        self.year_month_combo.setCurrentIndex(i)

    def _on_config(self) -> None:
        dlg = ChartConfigDialog(self, meta=self._build_meta())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._config_meta = dlg.meta
            self._refresh()

    def _on_export(self) -> None:
        if self._current_figure is None:
            QMessageBox.information(self, "Export", "No chart to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Image", "lj_chart.png",
            "PNG (*.png);;JPG (*.jpg)",
        )
        if not path:
            return
        try:
            # G.3: 默认 DPI=300；元信息由 _config_meta 决定（来自 ChartConfigDialog）
            meta = getattr(self, "_config_meta", None) or self._build_meta()
            if self._current_data_pair is not None:
                # L+H 双轴图：按当前 meta 重渲染整张双轴图
                data_l, data_h = self._current_data_pair
                fig = LJ.render_lj_chart_dual_axis(data_l, data_h, meta=meta)
            elif self._current_data is not None:
                fig = LJ.render_lj_chart(self._current_data, meta=meta)
            else:
                fig = self._current_figure
            LJ.save_png(fig, path)  # 默认 dpi=300
            QMessageBox.information(self, "Export", f"Saved to:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))


# ============================================================================
# Hover tooltip (matplotlib motion_notify_event + QToolTip)
# ============================================================================

_HOVER_OFFSET = 12


def attach_hover_tooltip(
    fig: Figure,
    canvas: FigureCanvasQTAgg,
    data: "LJ.LJChartData | Sequence[LJ.LJChartData]",
) -> None:
    """Attach a hover-driven QToolTip to the canvas.

    Behavior:
    - mouse moves over canvas -> find nearest point on the chart (x-axis by index
      of measurement, y-axis by value).
    - if within tolerance, show QToolTip with LJ.build_tooltip_text payload.
    - tooltips go away when mouse leaves the canvas.
    - data 可传单个 LJChartData（单水平）或列表（L+H 双轴图，列表顺序
      与 fig.axes 顺序一一对应：[L 左轴, H 右轴]）。

    Idempotent: re-calling replaces the previous listener.
    """
    # Normalize: single LJChartData or Sequence -> list aligned with fig.axes
    if isinstance(data, LJ.LJChartData):
        datas: list[LJ.LJChartData] = [data]
    else:
        datas = list(data)

    # Build per-axes point tables using the same layout as renderer
    ax_tables: list[tuple[Any, list[float], list[float], list[LJ.LJChartPoint]]] = []
    for ax, d in zip(fig.axes, datas):
        xs: list[float] = []
        ys: list[float] = []
        pts: list[LJ.LJChartPoint] = []
        for i, p in enumerate(d.points, start=1):
            xs.append(float(i))
            ys.append(p.value)
            pts.append(p)
        if xs:
            ax_tables.append((ax, xs, ys, pts))

    if not ax_tables:
        # Nothing to hover; detach any prior listener and return.
        cid = getattr(canvas, "_hover_cid", None)
        if cid is not None:
            try:
                canvas.mpl_disconnect(cid)
            except Exception:
                pass
            canvas._hover_cid = None
        return

    def _on_motion(event: Any) -> None:
        # Locate the axes the cursor is over; None -> hide tooltip
        table = None
        for candidate in ax_tables:
            if event is not None and event.inaxes is candidate[0]:
                table = candidate
                break
        if table is None:
            QToolTip.hideText()
            return
        ax, xs, ys, pts = table
        x_min, x_max = min(xs), max(xs)
        x_tol = max(1.0, (x_max - x_min) * 0.05)
        y_tol = max(0.5, (max(ys) - min(ys)) * 0.05) if len(set(ys)) > 1 else 1.0
        try:
            ex, ey = event.xdata, event.ydata
        except Exception:
            QToolTip.hideText()
            return
        if ex is None or ey is None:
            QToolTip.hideText()
            return
        # Find nearest by index (x is i = 1..N)
        nearest_i = min(range(len(xs)), key=lambda i: abs(xs[i] - ex))
        if abs(xs[nearest_i] - ex) > x_tol:
            QToolTip.hideText()
            return
        # Generous vertical tolerance: tooltip still useful even if y is
        # far (e.g. axis margin clicks).
        text = LJ.build_tooltip_text(pts[nearest_i])
        # Convert canvas-local pixel coords to global screen coords for
        # QToolTip. event has guiEvent (Qt) carrying the screen pos; fall
        # back to current cursor position if not available.
        global_pos = None
        gui = getattr(event, "guiEvent", None)
        if gui is not None:
            try:
                gp = gui.globalPos() if hasattr(gui, "globalPos") else None
                if gp is not None:
                    # PyQt6: globalPos() returns QPoint with .x()/.y()
                    global_pos = canvas.mapToGlobal(
                        canvas.pos()
                    )  # placeholder
                    global_pos = type(global_pos)(int(gp.x()), int(gp.y()))
            except Exception:
                global_pos = None
        if global_pos is None:
            try:
                global_pos = canvas.mapToGlobal(canvas.cursor().pos())
            except Exception:
                global_pos = canvas.cursor().pos()
        QToolTip.showText(global_pos, text)

    # Disconnect any prior listener
    prev = getattr(canvas, "_hover_cid", None)
    if prev is not None:
        try:
            canvas.mpl_disconnect(prev)
        except Exception:
            pass
    cid = canvas.mpl_connect("motion_notify_event", _on_motion)
    canvas._hover_cid = cid


# ============================================================================
# 点点击 → 排查面板（用户需求 2026-10-14：点击图上点，右侧展示排查信息）
# ============================================================================

_POINT_STATUS_LABEL: dict[str, str] = {
    "in_control": "在控",
    "warning": "警告",
    "out_of_control": "失控",
}

_POINT_STATUS_COLOR: dict[str, str] = {
    "in_control": "#334155",
    "warning": "#d9a400",
    "out_of_control": "#c0392b",
}


def attach_click_panel(
    fig: Figure,
    canvas: FigureCanvasQTAgg,
    data: "LJ.LJChartData | Sequence[LJ.LJChartData]",
    on_pick,
) -> None:
    """button_press_event → 命中最近数据点 → 回调 on_pick(point)。

    Behavior:
    - 与 attach_hover_tooltip 相同的最近点判定（x 轴按点序号，双轴图按水平
      分轴各建一张表，与 fig.axes 一一对应）；
    - 点击处距最近点的 x / y 都超出容差 → 忽略（避免误触面板）；
    - data 可传单个 LJChartData（单水平）或列表（L+H 双轴图）；
    - Idempotent: 重复调用替换旧监听；handler 存到 canvas._click_handler
      便于测试直接调用。
    """
    if isinstance(data, LJ.LJChartData):
        datas: list[LJ.LJChartData] = [data]
    else:
        datas = list(data)

    ax_tables: list[tuple[Any, list[float], list[float], list[LJ.LJChartPoint]]] = []
    for ax, d in zip(fig.axes, datas):
        xs: list[float] = []
        ys: list[float] = []
        pts: list[LJ.LJChartPoint] = []
        for i, p in enumerate(d.points, start=1):
            xs.append(float(i))
            ys.append(p.value)
            pts.append(p)
        if xs:
            ax_tables.append((ax, xs, ys, pts))

    if not ax_tables:
        prev = getattr(canvas, "_click_cid", None)
        if prev is not None:
            try:
                canvas.mpl_disconnect(prev)
            except Exception:
                pass
            canvas._click_cid = None
        canvas._click_handler = None
        return

    def _on_press(event: Any) -> None:
        table = None
        for candidate in ax_tables:
            if event is not None and event.inaxes is candidate[0]:
                table = candidate
                break
        if table is None:
            return
        _ax, xs, ys, pts = table
        ex = getattr(event, "xdata", None)
        ey = getattr(event, "ydata", None)
        if ex is None:
            return
        x_min, x_max = min(xs), max(xs)
        x_tol = max(1.0, (x_max - x_min) * 0.05)
        nearest_i = min(range(len(xs)), key=lambda i: abs(xs[i] - ex))
        if abs(xs[nearest_i] - ex) > x_tol:
            return
        if ey is not None:
            y_tol = max(0.5, (max(ys) - min(ys)) * 0.10) if len(set(ys)) > 1 else 1.0
            if abs(ys[nearest_i] - ey) > y_tol:
                return
        on_pick(pts[nearest_i])

    prev = getattr(canvas, "_click_cid", None)
    if prev is not None:
        try:
            canvas.mpl_disconnect(prev)
        except Exception:
            pass
    cid = canvas.mpl_connect("button_press_event", _on_press)
    canvas._click_cid = cid
    canvas._click_handler = _on_press  # 测试直接调用入口


__all__ = [
    "ChartConfigDialog",
    "LJChartDialog",
    "attach_hover_tooltip",
    "attach_click_panel",
]