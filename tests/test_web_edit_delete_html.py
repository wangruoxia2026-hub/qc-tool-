"""
Step 1+2 P1 partial: 验证 web HTML 编辑/删除 modal 接上 PUT/DELETE/POST

验收：
- saveEditProject 调 PUT /api/projects/<pid>
- deleteProject 调 DELETE /api/projects/<pid>
- saveEditTestItem 调 PUT /api/test_items/<tid>
- deleteTestItem 调 DELETE /api/test_items/<tid>
- saveAddLot 调 POST /api/test_items/<tid>/lots

跑法：
    python -m pytest tests/test_web_edit_delete_html.py -v
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
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _get_html(base: str) -> str:
    return urlreq.urlopen(f"{base}/", timeout=5).read().decode("utf-8")


def test_saveEditProject_calls_PUT(web_server):
    base, _ = web_server
    html = _get_html(base)
    idx = html.find("async function saveEditProject")
    assert idx > 0, "saveEditProject 未找到"
    chunk = html[idx:idx + 1500]
    assert "PUT" in chunk and "/api/projects/" in chunk, \
        "saveEditProject 没调 PUT /api/projects/"


def test_deleteProject_calls_DELETE(web_server):
    base, _ = web_server
    html = _get_html(base)
    idx = html.find("async function deleteProject")
    assert idx > 0
    chunk = html[idx:idx + 1500]
    assert "DELETE" in chunk and "/api/projects/" in chunk, \
        "deleteProject 没调 DELETE /api/projects/"


def test_saveEditTestItem_calls_PUT(web_server):
    base, _ = web_server
    html = _get_html(base)
    idx = html.find("async function saveEditTestItem")
    assert idx > 0, "saveEditTestItem 未找到"
    chunk = html[idx:idx + 1500]
    assert "PUT" in chunk and "/api/test_items/" in chunk, \
        "saveEditTestItem 没调 PUT /api/test_items/"


def test_deleteTestItem_calls_DELETE(web_server):
    base, _ = web_server
    html = _get_html(base)
    idx = html.find("async function deleteTestItem")
    assert idx > 0
    chunk = html[idx:idx + 1500]
    assert "DELETE" in chunk and "/api/test_items/" in chunk, \
        "deleteTestItem 没调 DELETE /api/test_items/"


def test_saveAddLot_calls_POST_lots(web_server):
    base, _ = web_server
    html = _get_html(base)
    idx = html.find("async function saveAddLot")
    assert idx > 0, "saveAddLot 未找到"
    chunk = html[idx:idx + 1500]
    assert "POST" in chunk and "/api/test_items/" in chunk and "/lots" in chunk, \
        "saveAddLot 没调 POST /api/test_items/.../lots"