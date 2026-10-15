"""
单条录入增强功能测试（PyQt 桌面端 + Web 端）
遵循 AGENTS.md 铁律 5（测试先行）：
1. 序号默认从 1 开始，保存后自动递增；
2. 操作人员与前处理人员带入项目新建时的人员，支持下拉选择；
3. 不需要手动选择水平，同时展现高低水平测定值输入，支持同时录入保存。
"""

import sqlite3
from pathlib import Path
import pytest
from PyQt6.QtWidgets import QApplication

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool.main_window_b import EntryForm, MainWindowB


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "qc_test.db"
    init_database(db_file)
    conn = get_connection(db_file)
    conn.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, '测试检验科')")
    p = P.create_project(conn, name="生化五项", short="BIO5", unit="mmol/L", manufacturer="迈瑞")
    P.create_test_item(
        conn,
        project_id=p.id,
        name="葡萄糖",
        short="GLU",
        unit="mmol/L",
        lot="LOT2026",
        level="L",
        target=5.0,
        sd=0.2,
        operator="张三",
        pre_processor="李四",
    )
    P.create_test_item(
        conn,
        project_id=p.id,
        name="葡萄糖",
        short="GLU",
        unit="mmol/L",
        lot="LOT2026",
        level="H",
        target=15.0,
        sd=0.6,
        operator="张三",
        pre_processor="李四",
    )
    conn.commit()
    return conn, p.id


class TestDesktopEntryFormEnhanced:
    """PyQt 桌面端 EntryForm 增强测试"""

    def test_seq_defaults_to_one(self, qapp, test_db):
        conn, pid = test_db
        form = EntryForm()
        # 1. 序号默认从 1 开始
        assert form.seq_spin.value() == 1

    def test_personnel_populated_with_project_defaults(self, qapp, test_db):
        conn, pid = test_db
        items = P.list_test_items(conn, pid)
        form = EntryForm()
        form.set_project("生化五项", items)

        # 2. 操作人员和前处理人员下拉选项包含建档人员
        op_texts = [form.operator_combo.itemText(i) for i in range(form.operator_combo.count())]
        pp_texts = [form.pre_processor_combo.itemText(i) for i in range(form.pre_processor_combo.count())]

        assert "张三" in op_texts
        assert "李四" in pp_texts
        # 且默认选建立项目时的人
        assert form.operator_combo.currentText() == "张三"
        assert form.pre_processor_combo.currentText() == "李四"

    def test_dual_level_values_concurrent_input(self, qapp, test_db):
        conn, pid = test_db
        items = P.list_test_items(conn, pid)
        form = EntryForm()
        form.set_project("生化五项", items)

        # 3. 同时提供高水平与低水平测定值输入框
        assert hasattr(form, "value_l_spin")
        assert hasattr(form, "value_h_spin")
        form.value_l_spin.setValue(5.1)
        form.value_h_spin.setValue(15.2)

        vals = form.values()
        # values() 应返回成对录入的两个记录信息（或包含 L 和 H）
        assert isinstance(vals, list)
        assert len(vals) == 2
        levels = {v["level"]: v["value"] for v in vals}
        assert levels["L"] == 5.1
        assert levels["H"] == 15.2
        assert vals[0]["operator"] == "张三"
        assert vals[0]["pre_processor"] == "李四"
        assert vals[0]["seq"] == 1

    def test_seq_auto_increments_after_save(self, qapp, test_db):
        conn, pid = test_db
        win = MainWindowB(conn=conn)
        win._refresh_projects()
        win.project_list.setCurrentRow(0)

        # 初始序号为 1
        assert win.entry_form.seq_spin.value() == 1
        win.entry_form.value_l_spin.setValue(5.0)
        win.entry_form.value_h_spin.setValue(15.0)

        win._on_save()

        # 保存后，序号自动递增为 2
        assert win.entry_form.seq_spin.value() == 2


class TestWebEntryFormHtmlContract:
    """Web 端静态模板中新 UI 元素与数据契约检查"""

    def test_html_contains_dual_level_and_datalist(self):
        tmpl = (Path(__file__).resolve().parent.parent / "qc_tool" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
        # 1. 序号默认从 1 开始
        assert 'id="ef-seq"' in tmpl
        # 2. 具备人员 datalist 或下拉
        assert 'id="ef-operator-list"' in tmpl or 'list="ef-operator-list"' in tmpl
        assert 'id="ef-pre-processor-list"' in tmpl or 'list="ef-pre-processor-list"' in tmpl
        # 3. 具备双水平输入框（高水平与低水平同时输入）
        assert 'id="ef-value-l"' in tmpl
        assert 'id="ef-value-h"' in tmpl
