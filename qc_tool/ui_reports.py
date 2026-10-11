"""
功能 G UI：月度质控报表 / 失控记录导出（spec V1.3 §G.1-G.2）

公开对话框：
- SelectMonthDialog       : 月报 — 项目 + 月份选择
- SelectIndicatorsDialog  : 月报 — 勾选导出指标（spec G.1 a）
- OOCExportDialog         : 失控记录导出 — 项目 + 时间段 + 格式（xlsx/pdf）

不在本期范围（spec §10）：自定义规则、跨水平对比图、打印预览等。

设计：
- 只读不写：UI 层只调用 qc_tool.reports 的纯函数，不改任何业务表
- 中文 UI（issue #2 "样式不追求好看"）
- 不依赖 QtDesigner；纯代码构建（与 ui_lj_chart / ui_main 一致）
"""
from __future__ import annotations

import io
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from . import projects as P
from . import reports as R


# ============================================================================
# G.1 月报 - 项目 + 月份选择
# ============================================================================

class SelectMonthDialog(QDialog):
    """月报入口：项目 + 月份 → 下一步选指标。"""

    def __init__(self, conn: sqlite3.Connection, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("生成月度质控报表 — 选择项目与月份")
        self.conn = conn
        self._project_id: Optional[int] = None
        self._project_name: str = ""
        self._year: int = 0
        self._month: int = 0

        layout = QVBoxLayout(self)

        # 项目下拉
        self.project_combo = QComboBox()
        for p in P.list_projects(conn):
            self.project_combo.addItem(p.name, userData=p.id)
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)

        # 月份下拉（默认当月）
        today = date.today()
        self.year_combo = QComboBox()
        for y in range(today.year - 2, today.year + 1):
            self.year_combo.addItem(str(y), userData=y)
        # 默认选当月
        for i in range(self.year_combo.count()):
            if self.year_combo.itemData(i) == today.year:
                self.year_combo.setCurrentIndex(i)
                break
        self.month_combo = QComboBox()
        for m in range(1, 13):
            self.month_combo.addItem(f"{m:02d}", userData=m)
        for i in range(self.month_combo.count()):
            if self.month_combo.itemData(i) == today.month:
                self.month_combo.setCurrentIndex(i)
                break

        form = QFormLayout()
        form.addRow("项目 *：", self.project_combo)
        year_month_row = QHBoxLayout()
        year_month_row.addWidget(self.year_combo)
        year_month_row.addWidget(QLabel("年"))
        year_month_row.addWidget(self.month_combo)
        year_month_row.addWidget(QLabel("月"))
        ym_widget = QWidget()
        ym_widget.setLayout(year_month_row)
        form.addRow("月份 *：", ym_widget)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_project_changed(self, _idx: int) -> None:
        pass

    def _on_accept(self) -> None:
        if self.project_combo.count() == 0:
            QMessageBox.warning(self, "提示", "请先创建项目")
            return
        self._project_id = self.project_combo.currentData()
        proj = P.get_project(self.conn, self._project_id)
        self._project_name = proj.name if proj else ""
        self._year = self.year_combo.currentData() or 0
        self._month = self.month_combo.currentData() or 0
        self.accept()

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def year(self) -> int:
        return self._year

    @property
    def month(self) -> int:
        return self._month


# ============================================================================
# G.1 月报 - 选择导出指标
# ============================================================================

class SelectIndicatorsDialog(QDialog):
    """spec G.1 a 弹窗：勾选要导出的「检测指标 + 水平」组合。

    验证示例：维生素D2-低 / 维生素D2-高 / 维生素D3-低 / 维生素D3-高 共 4 个。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        parent: Optional[QWidget] = None,
        *,
        project_name: str,
        year: int,
        month: int,
    ):
        super().__init__(parent)
        self.setWindowTitle("选择导出指标")
        self.conn = conn
        self._project_name = project_name
        self._year = year
        self._month = month
        self._selected_ids: list[int] = []

        layout = QVBoxLayout(self)
        info = QLabel(
            f"项目：<b>{project_name}</b>　月份：<b>{year:04d}-{month:02d}</b><br>"
            f"每个「检测指标 + 水平」为 1 个指标。至少勾选 1 个才能导出。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # spec G.1 b：月内无数据 → 提示
        if not R.monthly_has_data(
            conn, project_name=project_name, year=year, month=month,
        ):
            QMessageBox.information(
                self, "无数据",
                f"{project_name} 在 {year:04d}-{month:02d} 月内无数据。",
            )

        # 全选 / 全不选
        action_row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_none = QPushButton("全不选")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        action_row.addWidget(btn_all)
        action_row.addWidget(btn_none)
        action_row.addStretch()
        layout.addLayout(action_row)

        # 指标列表
        self.indicators = R.select_test_items_for_month(
            conn, project_name=project_name, year=year, month=month,
        )
        self._list = QListWidget()
        for it in self.indicators:
            qitem = QListWidgetItem(it.label)
            qitem.setData(Qt.ItemDataRole.UserRole, it.test_item_id)
            qitem.setFlags(qitem.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            qitem.setCheckState(Qt.CheckState.Unchecked)
            self._list.addItem(qitem)
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(state)

    def _on_accept(self) -> None:
        ids: list[int] = []
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                ids.append(it.data(Qt.ItemDataRole.UserRole))
        # spec G.1 c：不勾选任何指标 → 提示
        if not ids:
            QMessageBox.information(self, "提示", "请至少勾选一个指标")
            return
        self._selected_ids = ids
        self.accept()

    @property
    def selected_test_item_ids(self) -> list[int]:
        return list(self._selected_ids)


# ============================================================================
# G.2 失控记录导出 - 项目 + 时间段 + 格式
# ============================================================================

class OOCExportDialog(QDialog):
    """spec G.2：选项目 + 时间段 → 选 xlsx 或 pdf → 导出。"""

    def __init__(self, conn: sqlite3.Connection, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("失控记录导出")
        self.conn = conn
        self._project_name: str = ""
        self._date_from: str = ""
        self._date_to: str = ""
        self._format: str = "xlsx"  # 'xlsx' or 'pdf'

        layout = QVBoxLayout(self)

        # 项目
        self.project_combo = QComboBox()
        for p in P.list_projects(conn):
            self.project_combo.addItem(p.name, userData=p.id)

        # 时间段
        self.date_from_edit = QDateEdit()
        self.date_from_edit.setCalendarPopup(True)
        self.date_from_edit.setDisplayFormat("yyyy-MM-dd")
        # 默认当月 1 号 ~ 当月最后一天
        today = date.today()
        first_this = today.replace(day=1)
        # 上月最后一天 = first_this - 1
        from datetime import timedelta
        last_prev = first_this - timedelta(days=1)
        self.date_from_edit.setDate(QDate(last_prev.year, last_prev.month, 1))
        self.date_to_edit = QDateEdit()
        self.date_to_edit.setCalendarPopup(True)
        self.date_to_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_to_edit.setDate(QDate(last_prev.year, last_prev.month, last_prev.day))

        # 快捷按钮：本月 / 上月 / 单日
        quick_row = QHBoxLayout()
        btn_this_month = QPushButton("本月")
        btn_last_month = QPushButton("上月")
        btn_today = QPushButton("今日")
        for b, slot in [
            (btn_this_month, self._set_this_month),
            (btn_last_month, self._set_last_month),
            (btn_today, self._set_today),
        ]:
            b.clicked.connect(slot)
            quick_row.addWidget(b)
        quick_row.addStretch()
        layout.addLayout(quick_row)

        # 格式
        self.format_combo = QComboBox()
        self.format_combo.addItem("Excel (.xlsx)", userData="xlsx")
        self.format_combo.addItem("PDF (.pdf)", userData="pdf")

        form = QFormLayout()
        form.addRow("项目 *：", self.project_combo)
        form.addRow("起始日期 *：", self.date_from_edit)
        form.addRow("结束日期（含） *：", self.date_to_edit)
        form.addRow("导出格式 *：", self.format_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_this_month(self) -> None:
        today = date.today()
        first = today.replace(day=1)
        from calendar import monthrange
        last_day = monthrange(today.year, today.month)[1]
        last = today.replace(day=last_day)
        self.date_from_edit.setDate(QDate(first.year, first.month, first.day))
        self.date_to_edit.setDate(QDate(last.year, last.month, last.day))

    def _set_last_month(self) -> None:
        today = date.today()
        first_this = today.replace(day=1)
        from datetime import timedelta
        last_prev = first_this - timedelta(days=1)
        first = last_prev.replace(day=1)
        self.date_from_edit.setDate(QDate(first.year, first.month, first.day))
        self.date_to_edit.setDate(QDate(last_prev.year, last_prev.month, last_prev.day))

    def _set_today(self) -> None:
        today = date.today()
        self.date_from_edit.setDate(QDate(today.year, today.month, today.day))
        self.date_to_edit.setDate(QDate(today.year, today.month, today.day))

    def _on_accept(self) -> None:
        if self.project_combo.count() == 0:
            QMessageBox.warning(self, "提示", "请先创建项目")
            return
        pid = self.project_combo.currentData()
        proj = P.get_project(self.conn, pid)
        self._project_name = proj.name if proj else ""
        self._date_from = self.date_from_edit.date().toString("yyyy-MM-dd")
        self._date_to = self.date_to_edit.date().toString("yyyy-MM-dd")
        if self._date_from > self._date_to:
            QMessageBox.warning(self, "提示", "起始日期不能晚于结束日期")
            return
        self._format = self.format_combo.currentData() or "xlsx"
        self.accept()

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def date_from(self) -> str:
        return self._date_from

    @property
    def date_to(self) -> str:
        return self._date_to

    @property
    def format(self) -> str:
        return self._format


# ============================================================================
# 工具函数：导出流程
# ============================================================================

def run_monthly_report_export(
    conn: sqlite3.Connection,
    parent: Optional[QWidget] = None,
) -> bool:
    """UI 入口：弹两层对话框 + 保存 xlsx。返回是否成功导出。"""
    dlg1 = SelectMonthDialog(conn, parent=parent)
    if dlg1.exec() != QDialog.DialogCode.Accepted:
        return False
    dlg2 = SelectIndicatorsDialog(
        conn, parent=parent,
        project_name=dlg1.project_name,
        year=dlg1.year, month=dlg1.month,
    )
    if dlg2.exec() != QDialog.DialogCode.Accepted:
        return False
    default_name = f"monthly_{dlg1.project_name}_{dlg1.year:04d}-{dlg1.month:02d}.xlsx"
    path, _ = QFileDialog.getSaveFileName(
        parent, "保存月报", default_name, "Excel Files (*.xlsx)",
    )
    if not path:
        return False
    try:
        xlsx = R.build_monthly_report_xlsx(
            conn,
            project_name=dlg1.project_name,
            year=dlg1.year, month=dlg1.month,
            selected_test_item_ids=dlg2.selected_test_item_ids,
        )
        Path(path).write_bytes(xlsx)
    except R.NoIndicatorSelected:
        QMessageBox.information(parent, "提示", "请至少勾选一个指标")
        return False
    except R.ProjectNotFound as e:
        QMessageBox.warning(parent, "项目不存在", str(e))
        return False
    except Exception as e:
        QMessageBox.warning(parent, "导出失败", str(e))
        return False
    QMessageBox.information(parent, "导出成功", f"月报已保存到：\n{path}")
    return True


def run_ooc_export(
    conn: sqlite3.Connection,
    parent: Optional[QWidget] = None,
) -> bool:
    """UI 入口：弹对话框 + 导出 xlsx/pdf。"""
    dlg = OOCExportDialog(conn, parent=parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    if not R.has_ooc_in_range(
        conn,
        project_name=dlg.project_name,
        date_from=dlg.date_from, date_to_inclusive=dlg.date_to,
    ):
        QMessageBox.information(parent, "提示", "该时间段无失控记录")
        return False
    fmt = dlg.format
    default_name = (
        f"ooc_{dlg.project_name}_{dlg.date_from}_to_{dlg.date_to}.{fmt}"
    )
    if fmt == "xlsx":
        path, _ = QFileDialog.getSaveFileName(
            parent, "保存失控记录", default_name, "Excel Files (*.xlsx)",
        )
        if not path:
            return False
        try:
            xlsx = R.build_ooc_report_xlsx(
                conn, project_name=dlg.project_name,
                date_from=dlg.date_from, date_to_inclusive=dlg.date_to,
            )
            Path(path).write_bytes(xlsx)
        except Exception as e:
            QMessageBox.warning(parent, "导出失败", str(e))
            return False
    elif fmt == "pdf":
        path, _ = QFileDialog.getSaveFileName(
            parent, "保存失控记录", default_name, "PDF Files (*.pdf)",
        )
        if not path:
            return False
        try:
            pdf_bytes = R.build_ooc_report_pdf(
                conn, project_name=dlg.project_name,
                date_from=dlg.date_from, date_to_inclusive=dlg.date_to,
            )
            Path(path).write_bytes(pdf_bytes)
        except Exception as e:
            QMessageBox.warning(parent, "导出失败", str(e))
            return False
    else:
        QMessageBox.warning(parent, "提示", f"未知格式: {fmt}")
        return False
    QMessageBox.information(parent, "导出成功", f"失控记录已保存到：\n{path}")
    return True


__all__ = [
    "SelectMonthDialog",
    "SelectIndicatorsDialog",
    "OOCExportDialog",
    "run_monthly_report_export",
    "run_ooc_export",
]
