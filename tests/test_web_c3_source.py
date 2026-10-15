"""
PR#3 — spec V1.3 §C.3 source 固定/累计切换 UI 测试

覆盖：
1. HTML 编辑检测指标 modal 含 source 选择下拉框（id="m-e-ti-source"）及「固定」「累计」两项
2. saveEditTestItem JS 函数将 source 组装进提交的 payload 并发往 PUT /api/test_items/<id>
3. 端到端：PUT /api/test_items/<id> 带 source="累计" 能成功更新并持久化到 DB
"""
from __future__ import annotations

import json
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


def _http(method: str, url: str, data: dict | None = None) -> tuple[int, bytes]:
    body = json.dumps(data).encode("utf-8") if data is not None else b""
    req = urlreq.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"}
    )
    resp = urlreq.urlopen(req, timeout=5)
    return resp.status, resp.read()


def test_html_edit_ti_has_source_select(web_server):
    """静态扫描 HTML 存在 m-e-ti-source 及 固定/累计 选项。"""
    base, _ = web_server
    html = _get_html(base)
    assert 'id="m-e-ti-source"' in html, "HTML 缺 m-e-ti-source 下拉框"
    assert 'value="固定"' in html, "m-e-ti-source 缺 固定 选项"
    assert 'value="累计"' in html, "m-e-ti-source 缺 累计 选项"
    assert 'source: $("m-e-ti-source").value' in html, "saveEditTestItem 应传递 source 字段"


def test_put_test_item_source_cumulative(web_server):
    """端到端验证 PUT test_items 更新 source 字段成功。"""
    base, _ = web_server
    # 1. 建项目
    _, res = _http("POST", f"{base}/api/projects", {"name": "血糖项目"})
    pid = json.loads(res)["id"]
    # 2. 建检测指标
    _, res = _http("POST", f"{base}/api/projects/{pid}/test_items", {
        "name": "GLU", "lot": "LOT1", "level": "L", "target": 5.0, "sd": 0.2
    })
    tid = json.loads(res)["id"]
    # 3. 编辑指标：设置 source 为 累计
    status, res = _http("PUT", f"{base}/api/test_items/{tid}", {
        "target": 5.0, "sd": 0.2, "source": "累计"
    })
    assert status == 200
    updated = json.loads(res)
    assert updated["source"] == "累计"

    # 4. 获取指标列表再次验证持久化
    status, res = _http("GET", f"{base}/api/projects/{pid}/test_items")
    assert status == 200
    items = json.loads(res)
    assert len(items) == 1
    assert items[0]["source"] == "累计"
