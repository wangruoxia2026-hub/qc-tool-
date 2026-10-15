"""
PR#6 — spec V1.3 §G.3 导出当前质控图 PNG 测试

覆盖：
1. HTML 包含导出图片按钮（id="btn-export-png"）
2. HTML JS 包含 exportChartPng 函数
3. exportChartPng 函数完整实现 SVG -> Canvas -> PNG 并触发 a[download] 下载
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


def test_html_has_export_png_button_and_logic(web_server):
    """验证 HTML 存在导出图片按钮及 Canvas PNG 转换逻辑。"""
    base, _ = web_server
    html = _get_html(base)
    assert 'id="btn-export-png"' in html, "HTML 缺导出图片按钮 id=btn-export-png"
    assert "exportChartPng" in html, "HTML 缺 exportChartPng 函数"
    assert "createElement(\"canvas\")" in html, "exportChartPng 需使用 Canvas 渲染"
    assert "toDataURL(\"image/png\")" in html, "exportChartPng 需导出 PNG 格式"
    assert "a.download" in html, "exportChartPng 需触发 a.download 下载"
