"""
功能 B 主窗口（spec V1.3 §B + §F.1）

布局：
- 左：项目列表
- 中：当前项目下的检测指标列表
- 右（顶部）：录入表单（项目自动 / 指标自动 / 水平下拉 / 批号 / 日期 / 测定值 / 备注 / 保存）
- 右（下半）：当前指标的 measurements 列表（按日期+水平+seq 排序，含状态列）
  - 作废行：灰色 + 删除线
- 底部按钮：
  - 「保存」（按录入表单）
  - 「编辑」/「标记作废」（选中行）
  - 「下载模板」/「批量导入」
- 菜单：「从备份恢复…」

启动时调用 backup.startup_check_and_backup + mark_exit_dirty；
aboutToQuit 钩 mark_exit_clean（沿用 backup 模块的钩子）。

失控时自动弹 §F.1 排查面板对话框。
"""
from __future__ import annotations

import io
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QAction, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit,
    QDateEdit, QDoubleSpinBox, QComboBox, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QFileDialog, QInputDialog, QDialog,
    QDialogButtonBox, QFormLayout, QCheckBox, QScrollArea, QGroupBox,
)

from . import projects as P
from . import measurements as M
from . import excel_io as X
from . import investigation as I
from . import backup
from .db import get_connection, init_database


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "qc.db"


# ============================================================================
# 录入表单
# ============================================================================

class EntryForm(QWidget):
    """录入表单（嵌在主窗口右侧）。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)

        self.project_label = QLabel("（请在左侧选项目）")
        self.test_item_combo = QComboBox()
        self.level_combo = QComboBox()
        self.level_combo.addItems(["L", "H"])
        self.lot_edit = QLineEdit("LOT1")
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.seq_spin = QDoubleSpinBox()
        self.seq_spin.setRange(0, 999)
        self.seq_spin.setDecimals(0)
        self.seq_spin.setValue(0)
        self.value_spin = QDoubleSpinBox()
        self.value_spin.setRange(0.0, 1e9)
        self.value_spin.setDecimals(3)
        self.value_spin.setValue(100.0)
        self.note_edit = QLineEdit()

        layout.addRow("项目：", self.project_label)
        layout.addRow("检测指标：", self.test_item_combo)
        layout.addRow("水平：", self.level_combo)
        layout.addRow("批号：", self.lot_edit)
        layout.addRow("日期：", self.date_edit)
        layout.addRow("序号：", self.seq_spin)
        layout.addRow("测定值：", self.value_spin)
        layout.addRow("备注：", self.note_edit)

    def set_project(self, project_name: str, items: list[P.TestItem]):
        self.project_label.setText(project_name)
        self.test_item_combo.clear()
        for it in items:
            self.test_item_combo.addItem(
                f"{it.name} · {it.lot} · {it.level} · 靶值={it.target} SD={it.sd}",
                userData=(it.id, it.lot, it.level, it.target, it.sd),
            )

    def values(self) -> dict:
        data = self.test_item_combo.currentData()
        if data is None:
            return None
        tid, lot_default, level_default, _, _ = data
        return {
            "test_item_id": tid,
            "project_id": None,  # 主窗口补
            "lot": self.lot_edit.text().strip() or lot_default,
            "level": self.level_combo.currentText(),
            "date": self.date_edit.date().toString("yyyy-MM-dd"),
            "seq": int(self.seq_spin.value()),
            "value": float(self.value_spin.value()),
            "note": self.note_edit.text().strip() or None,
        }


# ============================================================================
# §F.1 排查面板对话框
# ============================================================================

class InvestigationDialog(QDialog):
    """spec §F.1 排查面板：按触发规则返回分类清单 + 可勾选 + conclusion。"""

    def __init__(self, parent=None, *, plan: dict, measurement_id: int):
        super().__init__(parent)
        self.setWindowTitle(f"失控排查 — 规则 {','.join(plan['context']['rules'])}")
        self.resize(640, 480)
        self._measurement_id = measurement_id
        self._plan = plan
        self._checkboxes: dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        ctx = plan["context"]
        info = QLabel(
            f"<b>{ctx['project_name']} / {ctx['test_item_name']}</b> "
            f"· {ctx['lot']} · {ctx['level']} · {ctx['triggered_date']} · "
            f"value={ctx['value']}"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        for cat in plan["categories"]:
            group = QGroupBox(cat["category"])
            g_layout = QVBoxLayout(group)
            for item_text in cat["items"]:
                key = f"{cat['category']}_{item_text}"
                cb = QCheckBox(item_text)
                self._checkboxes[key] = cb
                g_layout.addWidget(cb)
            body_layout.addWidget(group)
        scroll.setWidget(body)
        layout.addWidget(scroll)

        self.conclusion_edit = QLineEdit()
        self.conclusion_edit.setPlaceholderText("处理结论（可选）")
        layout.addWidget(QLabel("处理结论："))
        layout.addWidget(self.conclusion_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self):
        checklist = {k: cb.isChecked() for k, cb in self._checkboxes.items()}
        if not any(checklist.values()):
            QMessageBox.warning(
                self, "已检查项为必填",
                "请至少勾选一项检查项再保存（spec §F.1 d）。",
            )
            return
        try:
            I.save_investigation(
                self.parent().conn,
                measurement_id=self._measurement_id,
                rules=self._plan["context"]["rules"],
                checklist=checklist,
                conclusion=self.conclusion_edit.text().strip() or None,
            )
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.accept()


# ============================================================================
# 主窗口
# ============================================================================

class MainWindowB(QMainWindow):
    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self.conn = conn
        self.setWindowTitle("临床检验室内质控工具 V1.3 — 功能 B 数据录入")
        self.resize(1200, 720)

        self._current_project_id: Optional[int] = None
        self._current_test_item_id: Optional[int] = None

        # 左侧：项目 + 检测指标
        self.project_list = QListWidget()
        self.project_list.currentItemChanged.connect(self._on_project_selected)
        self.test_item_list = QListWidget()
        self.test_item_list.currentItemChanged.connect(self._on_test_item_selected)

        left = QVBoxLayout()
        left.addWidget(QLabel("检验项目"))
        left.addWidget(self.project_list)
        left.addWidget(QLabel("检测指标"))
        left.addWidget(self.test_item_list)

        # 右侧：录入表单 + 测量表
        self.entry_form = EntryForm()
        self.measurements_table = QTableWidget()
        self.measurements_table.setColumnCount(8)
        self.measurements_table.setHorizontalHeaderLabels([
            "id", "日期", "水平", "批号", "序号", "测定值", "状态", "规则",
        ])
        self.measurements_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.measurements_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows,
        )
        self.measurements_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch,
        )

        right = QVBoxLayout()
        right.addWidget(QLabel("录入表单"))
        right.addWidget(self.entry_form)
        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("保存")
        self.btn_save.clicked.connect(self._on_save)
        self.btn_void = QPushButton("标记作废")
        self.btn_void.clicked.connect(self._on_void)
        self.btn_edit_value = QPushButton("编辑值")
        self.btn_edit_value.clicked.connect(self._on_edit_value)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_void)
        btn_row.addWidget(self.btn_edit_value)
        right.addLayout(btn_row)
        right.addWidget(QLabel("测量列表（灰色 + 删除线 = 作废）"))
        right.addWidget(self.measurements_table)

        import_row = QHBoxLayout()
        self.btn_template = QPushButton("下载模板")
        self.btn_import = QPushButton("批量导入")
        self.btn_template.clicked.connect(self._on_download_template)
        self.btn_import.clicked.connect(self._on_import)
        import_row.addWidget(self.btn_template)
        import_row.addWidget(self.btn_import)
        right.addLayout(import_row)

        # 菜单
        self._build_menu()

        # 整体布局
        central = QWidget()
        body = QHBoxLayout(central)
        from PyQt6.QtWidgets import QWidget as _QW
        body.addLayout(left, 1)
        body.addLayout(right, 2)
        self.setCentralWidget(central)

        self._refresh_projects()

    def _build_menu(self):
        menu = self.menuBar().addMenu("系统")
        desc = backup.restore_menu_descriptor()
        act = QAction(desc["label"], self)
        def _on_restore():
            try:
                desc["handler"]()
            except backup.RestoreNotImplemented as e:
                QMessageBox.information(self, "功能建设中", str(e))
        act.triggered.connect(_on_restore)
        menu.addAction(act)

    # ----- 刷新 -----

    def _refresh_projects(self):
        self.project_list.clear()
        for p in P.list_projects(self.conn):
            it = QListWidgetItem(p.name)
            it.setData(Qt.ItemDataRole.UserRole, p.id)
            self.project_list.addItem(it)
        if self.project_list.count() > 0:
            self.project_list.setCurrentRow(0)

    def _refresh_test_items(self, project_id: int):
        self.test_item_list.clear()
        for ti in P.list_test_items(self.conn, project_id):
            it = QListWidgetItem(f"{ti.name} · {ti.lot} · {ti.level}")
            it.setData(Qt.ItemDataRole.UserRole, ti.id)
            self.test_item_list.addItem(it)

    def _refresh_measurements(self, test_item_id: int):
        rows = M.list_measurements(self.conn, test_item_id, include_void=True)
        self.measurements_table.setRowCount(len(rows))
        for i, m in enumerate(rows):
            self.measurements_table.setItem(i, 0, QTableWidgetItem(str(m.id)))
            self.measurements_table.setItem(i, 1, QTableWidgetItem(m.date))
            self.measurements_table.setItem(i, 2, QTableWidgetItem(m.level))
            self.measurements_table.setItem(i, 3, QTableWidgetItem(m.lot))
            self.measurements_table.setItem(i, 4, QTableWidgetItem(str(m.seq)))
            val_item = QTableWidgetItem(f"{m.value:.3f}")
            val_item.setData(Qt.ItemDataRole.UserRole, m.id)
            self.measurements_table.setItem(i, 5, val_item)
            status_item = QTableWidgetItem(m.status or "")
            rules_item = QTableWidgetItem(m.rules or "")
            # 状态着色
            if m.status == "作废":
                font = QFont()
                font.setStrikeOut(True)
                for col in range(8):
                    cell = self.measurements_table.item(i, col)
                    if cell is not None:
                        cell.setFont(font)
                        cell.setForeground(QColor("gray"))
            elif m.status and m.status.startswith("失控"):
                for col in (5, 6):
                    cell = self.measurements_table.item(i, col)
                    if cell is not None:
                        cell.setForeground(QColor("red"))
            elif m.status and m.status.startswith("警告"):
                for col in (5, 6):
                    cell = self.measurements_table.item(i, col)
                    if cell is not None:
                        cell.setForeground(QColor("#c8a415"))
            self.measurements_table.setItem(i, 6, status_item)
            self.measurements_table.setItem(i, 7, rules_item)

    # ----- 选择事件 -----

    def _on_project_selected(self, current, _prev):
        if current is None:
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        self._current_project_id = pid
        proj = P.get_project(self.conn, pid)
        items = P.list_test_items(self.conn, pid)
        self.entry_form.set_project(proj.name if proj else "?", items)
        self._refresh_test_items(pid)

    def _on_test_item_selected(self, current, _prev):
        if current is None:
            return
        tid = current.data(Qt.ItemDataRole.UserRole)
        self._current_test_item_id = tid
        self._refresh_measurements(tid)

    # ----- 按钮事件 -----

    def _on_save(self):
        if self._current_project_id is None or self._current_test_item_id is None:
            QMessageBox.information(self, "提示", "请先在左侧选项目与检测指标")
            return
        v = self.entry_form.values()
        if v is None:
            QMessageBox.information(self, "提示", "录入表单不完整")
            return
        v["project_id"] = self._current_project_id
        try:
            # 日期提示（spec §9）
            date_info = M.validate_measurement_date(v["date"])
            if date_info["is_future"]:
                if QMessageBox.question(
                    self, "日期晚于今天",
                    f"日期 {v['date']} 晚于今天，是否仍要保存？",
                ) != QMessageBox.StandardButton.Yes:
                    return
            elif date_info["is_too_old"]:
                QMessageBox.information(
                    self, "日期较久",
                    f"日期 {v['date']} 早于当前日期 2 年以上；继续保存。",
                )
            m = M.create_measurement(self.conn, **v)
            self.conn.commit()
        except (M.ValueMustNonNegative, M.ValueInvalidNumber) as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        self._refresh_measurements(self._current_test_item_id)

        # 失控 → 弹 §F.1 排查面板
        if m.status and m.status.startswith("失控"):
            self._open_investigation_dialog(m, v)

    def _on_void(self):
        row = self.measurements_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一行")
            return
        mid = int(self.measurements_table.item(row, 0).text())
        m = M.get_measurement(self.conn, mid)
        if m is None or m.status == "作废":
            return
        if QMessageBox.question(
            self, "标记作废", f"将作废 id={mid} value={m.value} ?",
        ) != QMessageBox.StandardButton.Yes:
            return
        M.mark_void(self.conn, mid)
        self.conn.commit()
        self._refresh_measurements(self._current_test_item_id)

    def _on_edit_value(self):
        row = self.measurements_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一行")
            return
        mid = int(self.measurements_table.item(row, 0).text())
        m = M.get_measurement(self.conn, mid)
        if m is None or m.status == "作废":
            QMessageBox.information(self, "提示", "已作废或不存在")
            return
        new_val, ok = QInputDialog.getDouble(
            self, "编辑值", f"id={mid} 当前值={m.value}：",
            m.value, 0, 1e9, 3,
        )
        if not ok:
            return
        M.update_measurement_value(self.conn, mid, value=new_val)
        self.conn.commit()
        self._refresh_measurements(self._current_test_item_id)

    def _on_download_template(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存模板", "qc_template.xlsx",
            "Excel Files (*.xlsx)",
        )
        if not path:
            return
        xlsx = X.build_template_xlsx()
        Path(path).write_bytes(xlsx)
        QMessageBox.information(self, "成功", f"模板已保存到 {path}")

    def _on_import(self):
        """批量导入：选文件 → 解析 → 预览（可导出错误报告）→ 确认 → 入库。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Excel 文件", "",
            "Excel Files (*.xlsx)",
        )
        if not path:
            return
        xlsx_bytes = Path(path).read_bytes()
        try:
            plan = X.parse_import_xlsx_bytes(xlsx_bytes)
        except M.MissingRequiredColumns as e:
            QMessageBox.warning(
                self, "导入失败",
                f"缺少必需列：{', '.join(e.columns)}\n\n整批拒绝，请修正 Excel 后重新导入。",
            )
            return

        # 预览对话框
        dlg = _ImportPreviewDialog(self, plan)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # 确认导入
        def project_resolver(name: str):
            for p in P.list_projects(self.conn):
                if p.name == name:
                    return p.id
            return None

        # 解析 test_item_id 并填入 rows_for_dup
        ti_cache: dict[tuple[str, str, str], int] = {}
        rows_for_dup: list[dict] = []
        for r in plan.ok_rows:
            pid = project_resolver(r.project_name)
            if pid is None:
                QMessageBox.warning(
                    self, "导入失败",
                    f"项目 {r.project_name} 不存在，请先创建项目。",
                )
                return
            key = (r.test_item_name, r.project_name, r.level)
            if key not in ti_cache:
                items = P.list_test_items(self.conn, pid, name=r.test_item_name)
                tid = next((it.id for it in items if it.level == r.level), None)
                if tid is None:
                    QMessageBox.warning(
                        self, "导入失败",
                        f"检测指标 {r.test_item_name}（{r.level}）"
                        f"不存在（项目 {r.project_name}），请先创建检测指标。",
                    )
                    return
                ti_cache[key] = tid
            rows_for_dup.append({
                "test_item_id": ti_cache[key],
                "level": r.level,
                "lot": r.lot,
                "date": r.date,
                "seq": r.seq,
            })

        dups = M.find_duplicates_in_import(self.conn, rows_for_dup)
        overwrite = dlg.overwrite_mode()
        if dups and not overwrite:
            QMessageBox.information(self, "导入", "用户取消（发现重复但未选覆盖）。")
            return

        try:
            result = X.import_rows(
                self.conn, plan,
                project_resolver=project_resolver,
                overwrite=overwrite,
            )
        except X.ProjectNotFound as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        except X.TestItemNotFound as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        except M.DuplicateImportRows as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return

        self.conn.commit()
        QMessageBox.information(
            self, "导入完成",
            f"成功导入 {result['imported_count']} 条；\n"
            f"跳过 {result['skipped_count']} 行（详见错误报告文件）。",
        )
        if self._current_test_item_id is not None:
            self._refresh_measurements(self._current_test_item_id)


# ============================================================================
# 导入预览对话框（支持导出错误报告）
# ============================================================================

class _ImportPreviewDialog(QDialog):
    """导入预览：显示成功/跳过行统计，提供「导出错误报告」按钮和「确认/取消」按钮。"""

    def __init__(self, parent, plan: X.ImportPlan):
        super().__init__(parent)
        self.setWindowTitle("导入预览")
        self.resize(520, 300)
        self._plan = plan
        self._overwrite = False

        layout = QVBoxLayout(self)

        # 摘要
        ok = len(plan.ok_rows)
        skip = len(plan.skipped_rows)
        summary = QLabel(
            f"<b>成功 {ok} 行</b>，<b>跳过 {skip} 行</b>。"
            if ok > 0 else
            f"<b>成功 0 行</b>，跳过 {skip} 行。"
        )
        summary.setStyleSheet("font-size: 13px; padding: 6px;")
        layout.addWidget(summary)

        # 错误行列表（只显示前 20 条）
        if plan.skipped_rows:
            error_label = QLabel(f"跳过行（前 {min(20, skip)} 条）：")
            error_label.setStyleSheet("font-weight: bold; color: #c0392b;")
            layout.addWidget(error_label)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            error_widget = QWidget()
            err_layout = QVBoxLayout(error_widget)
            for sr in sorted(plan.skipped_rows, key=lambda s: s.original_row_no)[:20]:
                lbl = QLabel(sr.line_report())
                lbl.setStyleSheet("color: #c0392b; font-family: monospace;")
                err_layout.addWidget(lbl)
            if skip > 20:
                err_layout.addWidget(
                    QLabel(f"  … 共 {skip} 行，已截断。")
                )
            scroll.setWidget(error_widget)
            layout.addWidget(scroll, 1)
        else:
            layout.addWidget(QLabel("✅ 无跳过行，全部符合格式。"))

        # 按钮行
        btn_layout = QHBoxLayout()
        self._overwrite_chk = QCheckBox("覆盖已有记录")
        btn_layout.addWidget(self._overwrite_chk)

        export_btn = QPushButton("导出错误报告…")
        export_btn.clicked.connect(self._export_error_report)
        btn_layout.addWidget(export_btn)

        btn_layout.addStretch()
        ok_btn = QPushButton("确认导入")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def overwrite_mode(self) -> bool:
        return self._overwrite_chk.isChecked()

    def _export_error_report(self):
        """按要求 4：导出错误报告文件，用户照着行号改 Excel。"""
        path, _ = QFileDialog.getSaveFileName(
            self, "保存错误报告",
            f"import_errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)",
        )
        if not path:
            return
        report_text = X.export_error_report(self._plan, save_to=path)
        QMessageBox.information(
            self, "导出成功",
            f"错误报告已保存到：\n{path}\n\n"
            f"用户照着行号修改 Excel 后，重新导入即可。",
        )

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # ----- 排查面板 -----

    def _open_investigation_dialog(self, m: M.Measurement, v: dict):
        ti = P.get_test_item(self.conn, m.test_item_id)
        proj = P.get_project(self.conn, m.project_id) if m.project_id else None
        rules = []
        if m.rules:
            import json
            rules = json.loads(m.rules)
        if not rules:
            return
        plan = I.build_investigation_plan(
            rules=rules,
            triggered_date=m.date,
            project_name=proj.name if proj else "?",
            test_item_name=ti.name if ti else "?",
            lot=m.lot,
            level=m.level,
            value=m.value,
        )
        dlg = InvestigationDialog(self, plan=plan, measurement_id=m.id)
        dlg.exec()


# ============================================================================
# 入口
# ============================================================================

def _ensure_database(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    init_database(db_path)
    conn = get_connection(db_path)
    n = conn.execute("SELECT COUNT(*) FROM lab_settings").fetchone()[0]
    if n == 0:
        conn.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, NULL)")
        conn.commit()
    return conn


def main(argv: list[str] | None = None) -> int:
    from PyQt6.QtWidgets import QApplication
    argv = argv if argv is not None else []
    app = QApplication.instance() or QApplication(argv)

    db_path = _default_db_path()
    conn = _ensure_database(db_path)

    # 启动时备份
    backup.startup_check_and_backup(db_path)
    # 标记本次运行可能异常退出
    backup.mark_exit_dirty(db_path.parent)

    win = MainWindowB(conn)
    win.show()
    app.aboutToQuit.connect(lambda: backup.mark_exit_clean(db_path.parent))

    return app.exec()


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))