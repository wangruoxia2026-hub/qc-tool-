"""
功能 A 最简 GUI（PyQt6）—— spec §A.1–A.5

不在本期范围：图表、数据录入/导入/导出、绘图。所有 UI 功能仅"新建/编辑/删除
项目 + 新建/编辑/删除检测指标 + 添加新批号 + 系统设置（实验室名称）"。

风格：最简。样式不追求好看（issue #2 要求"样式不追求好看"）。
- 左侧：项目列表
- 中部：当前项目下的检测指标列表
- 按钮：新建项目 / 编辑项目 / 删除项目 / 添加检测指标 / 编辑检测指标
       / 删除检测指标 / 添加新批号 / 系统设置
- 启动时若 is_first_project_pending 自动弹"系统设置"对话框

不依赖 qt6 的 resource/qrc；纯代码构建。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QLabel,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QDoubleSpinBox,
    QComboBox,
    QMessageBox,
    QInputDialog,
    QTableWidget,
    QTableWidgetItem,
)

from . import projects as P
from .db import get_connection, init_database
from .statistics import get_statistics_view, Stats


# ============================================================================
# 默认 DB 路径 —— 与 init_db.py 保持一致（程序根目录/qc.db）
# ============================================================================

def _default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "qc.db"


# ============================================================================
# 表单对话框
# ============================================================================

class ProjectDialog(QDialog):
    """新建/编辑项目对话框。

    字段：name（必填）、short、unit、manufacturer。
    校验：name 非空（trim 后）；空则 LabNameEmpty 抛
    """

    def __init__(self, parent=None, *, project: Optional[P.Project] = None):
        super().__init__(parent)
        self.setWindowTitle("编辑项目" if project else "新建项目")
        self._project = project

        self.name_edit = QLineEdit(project.name if project else "")
        self.short_edit = QLineEdit(project.short if project else "")
        self.unit_edit = QLineEdit(project.unit if project else "")
        self.manufacturer_edit = QLineEdit(project.manufacturer if project else "")

        form = QFormLayout()
        form.addRow("项目名称 *", self.name_edit)
        form.addRow("简称", self.short_edit)
        form.addRow("单位", self.unit_edit)
        form.addRow("厂家", self.manufacturer_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self):
        # 走 validate_project 字段校验（GUI 弹框）
        try:
            P.validate_project(
                name=self.name_edit.text(),
                short=self.short_edit.text() or None,
                unit=self.unit_edit.text() or None,
                manufacturer=self.manufacturer_edit.text() or None,
            )
        except P.ProjectNameEmpty as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        self.accept()

    def values(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "short": self.short_edit.text().strip() or None,
            "unit": self.unit_edit.text().strip() or None,
            "manufacturer": self.manufacturer_edit.text().strip() or None,
        }


class TestItemDialog(QDialog):
    """新建/编辑检测指标对话框。"""

    def __init__(
        self,
        parent=None,
        *,
        project_id: int,
        test_item: Optional[P.TestItem] = None,
        default_name: str = "",
        default_lot: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle("编辑检测指标" if test_item else "新建检测指标")
        self._test_item = test_item
        self._project_id = project_id

        self.name_edit = QLineEdit(
            (test_item.name if test_item else default_name)
        )
        self.lot_edit = QLineEdit(
            (test_item.lot if test_item else default_lot) or ""
        )
        self.level_combo = QComboBox()
        self.level_combo.addItems(["L", "H"])
        if test_item is not None:
            idx = self.level_combo.findText(test_item.level)
            if idx >= 0:
                self.level_combo.setCurrentIndex(idx)

        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(0.001, 1e9)
        self.target_spin.setDecimals(3)
        self.target_spin.setValue(test_item.target if test_item else 100.0)

        self.sd_spin = QDoubleSpinBox()
        self.sd_spin.setRange(0.001, 1e9)
        self.sd_spin.setDecimals(3)
        self.sd_spin.setValue(test_item.sd if test_item else 2.0)

        self.source_combo = QComboBox()
        self.source_combo.addItems(["固定", "累计"])
        if test_item is not None:
            idx = self.source_combo.findText(test_item.source)
            if idx >= 0:
                self.source_combo.setCurrentIndex(idx)

        self.short_edit = QLineEdit(test_item.short if test_item else "")
        self.unit_edit = QLineEdit(test_item.unit if test_item else "")
        self.manufacturer_edit = QLineEdit(
            test_item.manufacturer if test_item else ""
        )

        form = QFormLayout()
        form.addRow("检测指标名称 *", self.name_edit)
        form.addRow("批号 *", self.lot_edit)
        form.addRow("水平 *", self.level_combo)
        form.addRow("靶值 *", self.target_spin)
        form.addRow("SD *", self.sd_spin)
        form.addRow("统计来源 *", self.source_combo)
        form.addRow("简称", self.short_edit)
        form.addRow("单位", self.unit_edit)
        form.addRow("厂家", self.manufacturer_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self):
        try:
            P.validate_test_item(
                name=self.name_edit.text(),
                lot=self.lot_edit.text(),
                level=self.level_combo.currentText(),
                target=self.target_spin.value(),
                sd=self.sd_spin.value(),
            )
        except P.ProjectNameEmpty as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        except P.TargetMustPositive as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        except P.SDMustPositive as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        except P.TargetInvalidNumber as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        except P.SDInvalidNumber as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        except P.LevelInvalid as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        self.accept()

    def values(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "lot": self.lot_edit.text().strip(),
            "level": self.level_combo.currentText(),
            "target": self.target_spin.value(),
            "sd": self.sd_spin.value(),
            "source": self.source_combo.currentText(),
            "short": self.short_edit.text().strip() or None,
            "unit": self.unit_edit.text().strip() or None,
            "manufacturer": self.manufacturer_edit.text().strip() or None,
        }


class AddLotDialog(QDialog):
    """添加新批号对话框（场景 11）。"""

    def __init__(self, parent=None, *, base: P.TestItem):
        super().__init__(parent)
        self.setWindowTitle(f"为 {base.name} 添加新批号")
        self._base = base

        self.lot_edit = QLineEdit()
        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(0.001, 1e9)
        self.target_spin.setDecimals(3)
        self.target_spin.setValue(base.target)
        self.sd_spin = QDoubleSpinBox()
        self.sd_spin.setRange(0.001, 1e9)
        self.sd_spin.setDecimals(3)
        self.sd_spin.setValue(base.sd)

        dim = "检测指标 + 批号 + 水平 + 日期"
        info = QLabel(
            f"检测指标：{base.name}（水平 {base.level}）\n"
            f"原批号：{base.lot}（靶值 {base.target} / SD {base.sd}）\n"
            f"新批号规则历史按{dim}四维从该批号起点清零。"
        )
        info.setWordWrap(True)

        form = QFormLayout()
        form.addRow("新批号 *", self.lot_edit)
        form.addRow("靶值 *", self.target_spin)
        form.addRow("SD *", self.sd_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self):
        name = self._base.name
        try:
            P.validate_test_item(
                name=name, lot=self.lot_edit.text(),
                level=self._base.level,
                target=self.target_spin.value(),
                sd=self.sd_spin.value(),
            )
        except Exception as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        self.accept()

    def values(self) -> dict:
        return {
            "new_lot": self.lot_edit.text().strip(),
            "target": self.target_spin.value(),
            "sd": self.sd_spin.value(),
        }


class LabSettingsDialog(QDialog):
    """系统设置 —— 实验室名称（场景 12 / spec §A.5）。"""

    def __init__(self, parent=None, *, current: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("系统设置 —— 实验室名称")
        self._name_edit = QLineEdit(current or "")
        info = QLabel(
            "实验室名称将出现在所有报表抬头和质控图元信息中。\n"
            "该字段必填，不能为空。"
        )
        info.setWordWrap(True)

        form = QFormLayout()
        form.addRow("实验室名称 *", self._name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self):
        try:
            P.validate_lab_name(name=self._name_edit.text())
        except P.LabNameEmpty as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        self.accept()

    def value(self) -> str:
        return self._name_edit.text().strip()


# ============================================================================
# 统计面板（功能 C GUI 增量）
# ============================================================================

class StatsPanel(QWidget):
    """
    统计区 widget，嵌入检测指标编辑区下方。

    布局：
    [统计来源: 固定 | 累计 ]
    |  |  N  |  均值  |  SD   |  CV% |
    |本月| .. |  ...  |  ...  | ... |
    |累计| .. |  ...  |  ...  | ... |
    """

    HEADER = ["", "N", "均值", "SD", "CV%"]

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._ti_id: Optional[int] = None

        # 统计来源切换
        self.source_combo = QComboBox()
        self.source_combo.addItems(["固定", "累计"])
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)

        # 两栏表格
        self.table = QTableWidget(2, 5)
        self.table.setHorizontalHeaderLabels(self.HEADER)
        self.table.setVerticalHeaderLabels(["本月", "累计"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFixedHeight(70)
        self.table.verticalHeader().setDefaultSectionSize(28)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("统计来源："))
        layout.addWidget(self.source_combo)
        layout.addWidget(self.table)
        self.setMinimumWidth(340)

    def _fmt(self, val: Optional[float]) -> str:
        """格式化数值，None → "—" """
        if val is None:
            return "—"
        return f"{val:.3f}"

    def load(self, ti_id: int) -> None:
        """加载指定检测指标的统计值。"""
        self._ti_id = ti_id
        # 同步 source 下拉（只 block 信号避免触发递归）
        ti = P.get_test_item(self.conn, ti_id)
        if ti is None:
            return
        idx = self.source_combo.findText(ti.source)
        if idx >= 0:
            self.source_combo.blockSignals(True)
            self.source_combo.setCurrentIndex(idx)
            self.source_combo.blockSignals(False)
        self._refresh()

    def _on_source_changed(self) -> None:
        """source 切换时同步写库并刷新。"""
        if self._ti_id is None:
            return
        new_source = self.source_combo.currentText()
        P.update_test_item(self.conn, self._ti_id, source=new_source)
        self._refresh()

    def _refresh(self) -> None:
        """重新计算并刷新表格。"""
        if self._ti_id is None:
            return
        ti = P.get_test_item(self.conn, self._ti_id)
        if ti is None:
            return
        view = get_statistics_view(self.conn, ti.id, ti.lot, ti.level)
        month = view["month"]
        cum = view["cumulative"]

        rows = [
            (month.n, month.mean, month.sd, month.cv_pct),
            (cum.n, cum.mean, cum.sd, cum.cv_pct),
        ]
        for r, (n, mean, sd, cv) in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(""))  # 空列
            self.table.setItem(r, 1, QTableWidgetItem(str(n)))
            self.table.setItem(r, 2, QTableWidgetItem(self._fmt(mean)))
            self.table.setItem(r, 3, QTableWidgetItem(self._fmt(sd)))
            self.table.setItem(r, 4, QTableWidgetItem(self._fmt(cv)))

    def clear(self) -> None:
        """清空显示（无选中指标时）。"""
        self._ti_id = None
        for r in range(2):
            for c in range(1, 5):
                self.table.setItem(r, c, QTableWidgetItem("—"))


# ============================================================================
# 主窗口
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setWindowTitle("临床检验室内质控工具 V1.3 —— 功能 A")
        self.resize(960, 600)

        # 项目列表
        self.project_list = QListWidget()
        self.project_list.currentItemChanged.connect(self._on_project_selected)

        # 检测指标列表
        self.test_item_list = QListWidget()
        self.test_item_list.currentItemChanged.connect(self._on_test_item_selected)

        # 状态：lab name
        self.status_label = QLabel()

        # 按钮（项目级）
        self.btn_new_project = QPushButton("新建项目")
        self.btn_edit_project = QPushButton("编辑项目")
        self.btn_delete_project = QPushButton("删除项目")
        self.btn_new_project.clicked.connect(self._on_new_project)
        self.btn_edit_project.clicked.connect(self._on_edit_project)
        self.btn_delete_project.clicked.connect(self._on_delete_project)

        # 按钮（检测指标级）
        self.btn_new_test_item = QPushButton("添加检测指标")
        self.btn_edit_test_item = QPushButton("编辑检测指标")
        self.btn_delete_test_item = QPushButton("删除检测指标")
        self.btn_add_lot = QPushButton("添加新批号")
        self.btn_new_test_item.clicked.connect(self._on_new_test_item)
        self.btn_edit_test_item.clicked.connect(self._on_edit_test_item)
        self.btn_delete_test_item.clicked.connect(self._on_delete_test_item)
        self.btn_add_lot.clicked.connect(self._on_add_lot)

        # 系统设置
        self.btn_settings = QPushButton("系统设置")
        self.btn_settings.clicked.connect(self._on_settings)

        # 统计面板
        self.stats_panel = StatsPanel(self.conn)
        self.stats_panel.clear()

        # 布局
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("检验项目"))
        left_layout.addWidget(self.project_list)
        proj_btn_layout = QHBoxLayout()
        proj_btn_layout.addWidget(self.btn_new_project)
        proj_btn_layout.addWidget(self.btn_edit_project)
        proj_btn_layout.addWidget(self.btn_delete_project)
        left_layout.addLayout(proj_btn_layout)

        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("检测指标"))
        right_layout.addWidget(self.test_item_list)
        ti_btn_layout = QHBoxLayout()
        ti_btn_layout.addWidget(self.btn_new_test_item)
        ti_btn_layout.addWidget(self.btn_edit_test_item)
        ti_btn_layout.addWidget(self.btn_delete_test_item)
        ti_btn_layout.addWidget(self.btn_add_lot)
        right_layout.addLayout(ti_btn_layout)
        right_layout.addWidget(self.status_label)
        right_layout.addWidget(self.btn_settings)
        right_layout.addWidget(self.stats_panel)

        central = QWidget()
        body = QHBoxLayout(central)
        body.addLayout(left_layout, 1)
        body.addLayout(right_layout, 1)
        self.setCentralWidget(central)

        self._refresh_projects()
        self._refresh_status_label()

    # ----- 刷新 -----

    def _refresh_projects(self):
        self.project_list.clear()
        for proj in P.list_projects(self.conn):
            it = QListWidgetItem(f"{proj.name}")
            it.setData(Qt.ItemDataRole.UserRole, proj.id)
            self.project_list.addItem(it)
        # 自动选中第一个
        if self.project_list.count() > 0:
            self.project_list.setCurrentRow(0)

    def _refresh_test_items(self, project_id: int):
        self.test_item_list.clear()
        for ti in P.list_test_items(self.conn, project_id):
            label = (
                f"{ti.name} · {ti.lot} · {ti.level} · "
                f"靶值={ti.target} SD={ti.sd}"
            )
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, ti.id)
            self.test_item_list.addItem(it)

    def _refresh_status_label(self):
        name = P.get_lab_name(self.conn) or "（未设置）"
        self.status_label.setText(f"当前实验室名称：{name}")

    # ----- 选择事件 -----

    def _on_project_selected(self, current: Optional[QListWidgetItem], _prev):
        if current is None:
            self.test_item_list.clear()
            self.stats_panel.clear()
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        self._refresh_test_items(pid)
        self.stats_panel.clear()

    def _on_test_item_selected(self, current: Optional[QListWidgetItem], _prev):
        if current is None:
            self.stats_panel.clear()
            return
        ti_id = current.data(Qt.ItemDataRole.UserRole)
        self.stats_panel.load(ti_id)

    def _select_test_item(self, ti_id: int) -> None:
        """内部用：刷新列表并选中指定 ti_id，再触发统计面板。"""
        self._refresh_test_items(self.conn.execute(
            "SELECT project_id FROM test_items WHERE id = ?", (ti_id,)
        ).fetchone()[0])
        for i in range(self.test_item_list.count()):
            if self.test_item_list.item(i).data(Qt.ItemDataRole.UserRole) == ti_id:
                self.test_item_list.setCurrentRow(i)
                return

    # ----- 项目按钮 -----

    def _on_new_project(self):
        dlg = ProjectDialog(self)
        if dlg.exec():
            v = dlg.values()
            try:
                P.create_project(
                    self.conn,
                    name=v["name"],
                    short=v["short"], unit=v["unit"], manufacturer=v["manufacturer"],
                )
            except P.ProjectNameEmpty as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.ProjectNameExists as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            self._refresh_projects()
        # 不可关闭的"必须先配实验室名称"逻辑放在启动时刻 → _ensure_lab_name

    def _on_edit_project(self):
        item = self.project_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先在左侧选中一个项目")
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        proj = P.get_project(self.conn, pid)
        if proj is None:
            return
        dlg = ProjectDialog(self, project=proj)
        if dlg.exec():
            v = dlg.values()
            try:
                P.update_project(
                    self.conn, pid,
                    name=v["name"], short=v["short"],
                    unit=v["unit"], manufacturer=v["manufacturer"],
                )
            except P.ProjectNameEmpty as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.ProjectNameExists as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            self._refresh_projects()

    def _on_delete_project(self):
        item = self.project_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先在左侧选中一个项目")
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        # 先做"项目级评估"看看有哪些检测指标/测量/失控记录
        proj = P.get_project(self.conn, pid)
        if proj is None:
            return
        # 二次确认
        confirm = QMessageBox.question(
            self, "确认删除",
            f"将删除项目「{proj.name}」及其下所有检测指标、相关 measurements 记录。"
            f"out_of_control 失控记录按 §8.3 c 留存（医疗合规）。\n"
            f"是否继续？",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            summary = P.delete_project(self.conn, pid)
            QMessageBox.information(
                self, "删除完成",
                f"项目已删除。\n"
                f"· 同步删除 {summary.n_test_items} 个检测指标\n"
                f"· 同步删除 {summary.n_measurements} 条 measurements\n"
                f"· {summary.n_out_of_control} 条失控记录已按 §8.3 c 留存",
            )
        except Exception as e:
            QMessageBox.warning(self, "错误", str(e))
            return
        self._refresh_projects()

    # ----- 检测指标按钮 -----

    def _on_new_test_item(self):
        item = self.project_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先在左侧选中一个项目")
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        dlg = TestItemDialog(self, project_id=pid)
        if dlg.exec():
            v = dlg.values()
            try:
                new_ti = P.create_test_item(
                    self.conn, pid,
                    name=v["name"], lot=v["lot"], level=v["level"],
                    target=v["target"], sd=v["sd"],
                    source=v["source"],
                    short=v["short"], unit=v["unit"], manufacturer=v["manufacturer"],
                )
            except P.ProjectNameEmpty as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.TargetMustPositive as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.SDMustPositive as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.TargetInvalidNumber as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.SDInvalidNumber as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.LevelInvalid as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            except P.LotExists as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            self._select_test_item(new_ti.id)

    def _on_edit_test_item(self):
        item = self.test_item_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先选中一个检测指标")
            return
        ti_id = item.data(Qt.ItemDataRole.UserRole)
        ti = P.get_test_item(self.conn, ti_id)
        if ti is None:
            return
        dlg = TestItemDialog(self, project_id=ti.project_id, test_item=ti)
        if dlg.exec():
            v = dlg.values()
            try:
                P.update_test_item(
                    self.conn, ti_id,
                    target=v["target"], sd=v["sd"],
                    source=v["source"],
                    short=v["short"], unit=v["unit"], manufacturer=v["manufacturer"],
                )
            except Exception as e:
                QMessageBox.warning(self, "错误", str(e))
                return
            self.stats_panel.load(ti_id)

    def _on_delete_test_item(self):
        item = self.test_item_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先选中一个检测指标")
            return
        ti_id = item.data(Qt.ItemDataRole.UserRole)
        # 场景 9：业务层主动检查 ooc
        try:
            summary = P.delete_test_item(self.conn, ti_id)
        except P.HasUnexportedOOC as e:
            QMessageBox.warning(self, "提示", str(e))
            return
        except LookupError as e:
            QMessageBox.warning(self, "提示", str(e))
            return
        QMessageBox.information(
            self, "删除完成",
            f"检测指标已删除。\n· 同步删除 {summary.n_measurements} 条 measurements\n"
            f"· out_of_control 失控记录按 §8.3 c 留存",
        )
        proj_item = self.project_list.currentItem()
        if proj_item is not None:
            pid = proj_item.data(Qt.ItemDataRole.UserRole)
            self._refresh_test_items(pid)

    def _on_add_lot(self):
        item = self.test_item_list.currentItem()
        if item is None:
            QMessageBox.information(self, "提示", "请先选中一个检测指标")
            return
        ti_id = item.data(Qt.ItemDataRole.UserRole)
        base = P.get_test_item(self.conn, ti_id)
        if base is None:
            return
        dlg = AddLotDialog(self, base=base)
        if dlg.exec():
            v = dlg.values()
            try:
                new_ti = P.add_lot(
                    self.conn, ti_id,
                    new_lot=v["new_lot"],
                    target=v["target"], sd=v["sd"],
                )
            except Exception as e:
                QMessageBox.warning(self, "错误", str(e))
                return
            proj_item = self.project_list.currentItem()
            if proj_item is not None:
                pid = proj_item.data(Qt.ItemDataRole.UserRole)
                self._refresh_test_items(pid)
            QMessageBox.information(
                self, "成功",
                f"新批号已添加（id={new_ti.id}）。\n"
                f"规则历史按 §D.3 四维（检测指标 + 批号 + 水平 + 日期）从该批号起点清零。",
            )

    def _on_settings(self):
        current = P.get_lab_name(self.conn)
        dlg = LabSettingsDialog(self, current=current)
        if dlg.exec():
            try:
                P.set_lab_name(self.conn, dlg.value())
            except P.LabNameEmpty as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
            self._refresh_status_label()


# ============================================================================
# 入口
# ============================================================================

def _ensure_database(db_path: Path):
    """按 spec §8.1 建库（如已有则跳过），返回就绪的连接。"""
    db_path = Path(db_path)
    init_database(db_path)
    conn = get_connection(db_path)
    n = conn.execute("SELECT COUNT(*) FROM lab_settings").fetchone()[0]
    if n == 0:
        conn.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, NULL)")
        conn.commit()
    return conn


def _ensure_lab_name(conn, parent=None) -> bool:
    """启动时若首次未设置实验室名称 → 强制弹窗。返回 True 表示已设置。"""
    if not P.is_first_project_pending(conn):
        return True
    QMessageBox.information(
        parent, "首次启动 —— 系统设置",
        "首次创建项目之前，请先在系统设置中填写实验室名称。\n该字段必填，不能为空。",
    )
    dlg = LabSettingsDialog(parent)
    if not dlg.exec():
        # 用户点取消 → 强制再次弹（lab_name 必须填）
        while not dlg.exec():
            pass
    try:
        P.set_lab_name(conn, dlg.value())
    except P.LabNameEmpty:
        # 验证已经做过；理论上走不到这里
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv
    app = QApplication(argv)

    db_path = _default_db_path()
    conn = _ensure_database(db_path)

    win = MainWindow(conn)
    win.show()

    # 启动时若 lab_name 缺失 → 强制设置
    _ensure_lab_name(conn, parent=win)
    win._refresh_status_label()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
