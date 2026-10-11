"""
Functional E UI: Levey-Jennings QC Chart dialog (spec V1.3 §E.1-E.2 + §10).

Public widgets
--------------
- ChartConfigDialog : 12 QCheckBox fields (spec §E.2 f)
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
4. Configurable 12 fields all default to visible; date_range is always-on
   fallback (lj_chart.make_default_meta enforces this).
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


# ============================================================================
# Chart config dialog (12 fields)
# ============================================================================

# Display labels for each field key (Chinese)
_FIELD_LABELS: dict[str, str] = {
    "project_name":    "Project Name",
    "test_item_name":  "Test Item",
    "test_item_short": "Short Name",
    "unit":            "Unit",
    "manufacturer":    "Manufacturer",
    "lot":             "Lot",
    "level":           "Level",
    "target":          "Target",
    "sd":              "SD",
    "operator":        "Operator",
    "pre_processor":   "Pre-processor",
    "date_range":      "Date Range",
}


class ChartConfigDialog(QDialog):
    """12-checkBox dialog for chart field visibility (spec §E.2 f)."""

    def __init__(self, parent: Optional[QWidget] = None, *,
                 meta: LJ.ChartMeta) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chart Configuration")
        self._meta = meta
        self._checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Tick which fields appear in the chart title area."
        ))

        for key in LJ.ALL_FIELDS:
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
        self.resize(1100, 720)

        # State
        self._current_data: Optional[LJ.LJChartData] = None
        self._current_figure: Optional[Figure] = None

        # ---- Top: filter dropdowns ----
        self.project_combo = QComboBox()
        self.test_item_combo = QComboBox()
        self.level_combo = QComboBox()
        self.level_combo.addItems(["L", "H"])
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

        body = QVBoxLayout()
        body.addLayout(filter_row)
        body.addLayout(nav_row)
        body.addWidget(self.canvas)

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
        _name, lot, level = data
        # Sync level_combo
        idx = self.level_combo.findText(level)
        if idx >= 0:
            self.level_combo.blockSignals(True)
            self.level_combo.setCurrentIndex(idx)
            self.level_combo.blockSignals(False)
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

    def _on_lot_changed(self, _idx: int) -> None:
        self._populate_months()

    def _populate_months(self) -> None:
        self.year_month_combo.blockSignals(True)
        self.year_month_combo.clear()
        months = self._filter_months(
            project_id=self.project_combo.currentData() or 0,
            test_item_name=self._current_test_item_name(),
            lot=self.lot_combo.currentData() or "",
            level=self.level_combo.currentText(),
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

        Per issue #5 user decision, void rows are still shown on the chart
        (4th style), so we don't filter them out at the month-list layer.
        """
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
        level = self.level_combo.currentText()
        ym = self.year_month_combo.currentData()
        if pid is None or not ti_name or not lot or ym is None:
            self._show_no_data()
            return
        year, month = ym
        data = LJ.compute_chart_data(
            self.conn, project_id=pid,
            test_item_name=ti_name, lot=lot, level=level,
            year=year, month=month,
        )
        self._current_data = data
        if not data.points:
            self._show_no_data()
            return
        meta = self._build_meta()
        fig = LJ.render_lj_chart(data, meta=meta)
        self._current_figure = fig
        self._swap_canvas(fig)
        attach_hover_tooltip(fig, self.canvas, data)

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
            fig = LJ.render_lj_chart(self._current_data, meta=meta) \
                if self._current_data is not None else self._current_figure
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
    data: LJ.LJChartData,
) -> None:
    """Attach a hover-driven QToolTip to the canvas.

    Behavior:
    - mouse moves over canvas -> find nearest point on the chart (x-axis by index
      of measurement, y-axis by value).
    - if within tolerance, show QToolTip with LJ.build_tooltip_text payload.
    - tooltips go away when mouse leaves the canvas.

    Idempotent: re-calling replaces the previous listener.
    """
    # Build a list of (x, y, point) using the same layout as renderer
    xs: list[float] = []
    ys: list[float] = []
    pts: list[LJ.LJChartPoint] = []
    for i, p in enumerate(data.points, start=1):
        xs.append(float(i))
        ys.append(p.value)
        pts.append(p)

    if not xs:
        # Nothing to hover; detach any prior listener and return.
        cid = getattr(canvas, "_hover_cid", None)
        if cid is not None:
            try:
                canvas.mpl_disconnect(cid)
            except Exception:
                pass
            canvas._hover_cid = None
        return

    ax = fig.axes[0]
    x_min, x_max = min(xs), max(xs)
    x_tol = max(1.0, (x_max - x_min) * 0.05)
    y_tol = max(0.5, (max(ys) - min(ys)) * 0.05) if len(set(ys)) > 1 else 1.0

    def _on_motion(event: Any) -> None:
        if event is None or event.inaxes is not ax:
            QToolTip.hideText()
            return
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


__all__ = [
    "ChartConfigDialog",
    "LJChartDialog",
    "attach_hover_tooltip",
]