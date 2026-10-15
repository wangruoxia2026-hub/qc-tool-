"""
PR#4 — spec V1.3 §E.1 d 图表配置勾选面板测试（新标题格式 6 项，2026-10-13）

覆盖：
1. HTML 含「图表配置」按钮（id="btn-chart-config"）及 openChartConfig 函数
2. openChartConfig 弹窗包含 6 项元信息 checkbox 及 全选 / 全不选 快捷操作
3. refreshChart 组装 &info= query 参数发往后端
4. 后端 GET /api/chart/<tid>.svg 支持 info query 过滤，按需控制 SVG 标题文本
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


def test_html_has_chart_config_button_and_6_checkboxes(web_server):
    """验证 HTML 存在图表配置入口、6 项配置与快捷全选/全不选。

    新标题格式（用户拍板 2026-10-13）：test_item_name/short/unit/
    manufacturer/level 不再上标题，配置项缩减为 6 个。
    """
    base, _ = web_server
    html = _get_html(base)
    assert 'id="btn-chart-config"' in html, "HTML 缺图表配置按钮 id=btn-chart-config"
    assert "openChartConfig" in html, "HTML 缺 openChartConfig 函数"
    assert "toggleAllChartConfig(true)" in html, "HTML 缺全选快捷操作"
    assert "toggleAllChartConfig(false)" in html, "HTML 缺全不选快捷操作"
    for field in [
        "project_name", "lot", "target", "sd",
        "operator", "pre_processor",
    ]:
        assert field in html, f"HTML 缺图表配置字段 {field}"


def test_GET_chart_svg_with_info_filter(web_server):
    """验证后端 GET /api/chart/<tid>.svg?info=... 能正确过滤元信息。"""
    base, _ = web_server
    # 建项目
    _, res = _http("POST", f"{base}/api/projects", {"name": "电解质"})
    pid = json.loads(res)["id"]
    # 建指标
    _, res = _http("POST", f"{base}/api/projects/{pid}/test_items", {
        "name": "K+", "lot": "LOT99", "level": "L", "target": 4.0, "sd": 0.2
    })
    tid = json.loads(res)["id"]

    # 1. 不带 info 参数（默认全开）→ 新标题格式逐行渲染
    url_all = f"{base}/api/chart/{tid}.svg?year=2026&month=4"
    resp_all = urlreq.urlopen(url_all, timeout=5).read().decode("utf-8")
    assert "电解质质控图" in resp_all
    assert 'font-size="24"' in resp_all, "首行应为 ×2 字号（12→24）"
    assert "质控品批号：LOT99" in resp_all
    assert "靶值（L）=4.000" in resp_all
    assert "SD（L）=0.200" in resp_all
    assert "时间范围：2026-04-01~2026-04-30" in resp_all
    # 新格式不再显示检测指标名
    assert "K+" not in resp_all

    # 2. 仅选 test_item_name（历史键，新格式无对应行）→ 只剩恒显的时间范围
    url_filtered = f"{base}/api/chart/{tid}.svg?year=2026&month=4&info=test_item_name"
    resp_filtered = urlreq.urlopen(url_filtered, timeout=5).read().decode("utf-8")
    assert "电解质" not in resp_filtered
    assert "靶值（L）" not in resp_filtered
    assert "时间范围：2026-04-01~2026-04-30" in resp_filtered
