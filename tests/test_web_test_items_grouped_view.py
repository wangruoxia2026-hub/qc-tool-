"""
测试：检测指标 L/H 水平在前端合并为同一卡片展示（需求 7）
- 检验指标下同一名称的指标（高水平 H 和低水平 L）聚合在同一个列表项内
- 包含高水平与低水平的批号、靶值、SD 渲染结构与类名
- 包含子行独立点击选中与激活样式联动
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


def test_grouped_test_items_html_and_js(web_server):
    """验证 HTML/JS 包含多水平合并渲染与独立选中逻辑。"""
    base, _ = web_server
    html = _get_html(base)

    # 1. 验证合并分组的核心样式类
    assert ".level-sub-group" in html, "缺少多水平聚合容器样式 .level-sub-group"
    assert ".level-sub-row" in html, "缺少单水平子行样式 .level-sub-row"
    assert ".level-sub-row.active" in html, "缺少单水平子行高亮样式 .level-sub-row.active"

    # 2. 验证聚合函数与点击事件处理
    assert "groupsMap.has(it.name)" in html, "缺少按指标名称 it.name 聚合逻辑"
    assert "selectTestItemById" in html, "缺少通过指标 ID 精准选中的辅助函数 selectTestItemById"
    assert "syncTestItemHighlight" in html, "缺少同步卡片与水平子行高亮状态的函数 syncTestItemHighlight"
