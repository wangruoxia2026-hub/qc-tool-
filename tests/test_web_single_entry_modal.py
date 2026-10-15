"""
自动化测试：单条录入弹窗字段必须从弹窗内读取
（2026-10-12 修复：隐藏"录入底座容器"与录入弹窗存在同名 id，getElementById
会取到文档中靠前的隐藏元素，导致弹窗里选的日期/批号/序号/人员/备注被忽略，
实际按隐藏框的默认值（如"今天"）存库。）
"""
from __future__ import annotations

import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlreq

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from qc_tool import web as webmod
    port = _free_port()
    handler = webmod.make_handler(db_path=tmp_path / "qc.db")
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.db_path = tmp_path / "qc.db"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    time.sleep(0.05)
    try:
        yield base
    finally:
        server.shutdown()


def test_entry_fields_read_from_open_modal(web_server):
    """submitEntry / resetEntry 必须经弹窗作用域读取字段（entryField）。"""
    base = web_server
    html = urlreq.urlopen(f"{base}/", timeout=5).read().decode("utf-8")

    # 弹窗作用域读取入口存在，且覆盖全部易冲突字段
    for expr in (
        'entryField("ef-date")',
        'entryField("ef-seq")',
        'entryField("ef-lot")',
        'entryField("ef-note")',
        'entryField("ef-operator")',
        'entryField("ef-pre-processor")',
        'entryField("ef-value-l")',
        'entryField("ef-value-h")',
    ):
        assert expr in html, f"录入字段应经弹窗作用域读取：{expr}"

    # 弹窗未打开时回退到隐藏底座（自动化脚本兼容路径）
    assert 'return $(id);' in html, "弹窗未打开时应回退 getElementById（隐藏底座路径）"


def test_modal_open_does_not_read_hidden_vault_date(web_server):
    """弹窗打开时，日期必须来自弹窗而非隐藏底座（回归：选 4 月却存 10 月）。"""
    base = web_server
    html = urlreq.urlopen(f"{base}/", timeout=5).read().decode("utf-8")

    # entryField 的弹窗判定依据弹窗内的 #ef-date，而不是文档级 getElementById
    assert 'modalBody.querySelector("#ef-date")' in html, \
        "弹窗作用域判定应查询弹窗内的 #ef-date"
    # 隐藏底座容器仍在（自动化脚本兼容），但弹窗路径不能再用 $("ef-date")
    assert 'id="headless-entry-vault"' in html
