"""
Step 1+2 P1 partial: 验证 web HTML 包含 spec 录入表单字段

- 录入表单含 ef-operator / ef-pre-processor（spec §0 名词表）
- 录入表单提交时这俩字段进 POST body

跑法：
    python -m pytest tests/test_web_entryform_html.py -v
"""
from __future__ import annotations

import json
import re
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
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def test_index_html_has_operator_input(web_server):
    """验收：HTML 含 ef-operator input。"""
    base, _ = web_server
    r = urlreq.urlopen(f"{base}/", timeout=5)
    html = r.read().decode("utf-8")
    assert 'id="ef-operator"' in html, "HTML 缺 ef-operator 字段"
    assert 'id="ef-pre-processor"' in html, "HTML 缺 ef-pre-processor 字段"
    assert "操作人员" in html, "HTML 缺 '操作人员' 标签"
    assert "前处理人员" in html, "HTML 缺 '前处理人员' 标签"


def test_index_html_submitEntry_sends_operator(web_server):
    """验收：submitEntry() JS 代码会读 ef-operator / ef-pre-processor。"""
    base, _ = web_server
    r = urlreq.urlopen(f"{base}/", timeout=5)
    html = r.read().decode("utf-8")
    # 简单静态扫描 JS 代码含这俩字段
    assert 'ef-operator' in html
    assert 'ef-pre-processor' in html
    # 在 submitEntry 函数体内引用（截取 submitEntry 起点后 2000 字符内
    # 一定能覆盖字段引用，又不依赖花括号配对）
    idx = html.find("async function submitEntry()")
    assert idx > 0
    chunk = html[idx:idx + 2000]
    assert "ef-operator" in chunk, "submitEntry 体内没读 ef-operator"
    assert "ef-pre-processor" in chunk, "submitEntry 体内没读 ef-pre-processor"
