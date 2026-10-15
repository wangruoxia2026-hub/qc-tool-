"""
自动化测试：Web 端质控图 L/H 单水平切换（用户需求 2026-10-14 第 4 条）
（点击检测指标 L/H 子行只显示该水平一条曲线；点击卡片空白处显示 L+H 双轴图）。

后端：/api/chart/<tid>.svg?scope=single 跳过配对只画该水平；
     scope 缺省 / scope=pair 维持 L+H 双轴（回归保护）。
前端：state.chartScope 在子行点击置 single、卡片空白点击置 pair，
     refreshChart 拼 &scope= 参数。

Run:
    python -m pytest tests/test_web_chart_scope.py -v
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

# 双轴图特有标记（web.py _dual_data_to_svg 的水平说明行）
DUAL_MARKER = "低水平 L（左轴） · 高水平 H（右轴）"


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


def _post_json(base: str, path: str, payload: dict) -> dict:
    req = urlreq.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urlreq.urlopen(req, timeout=5).read().decode("utf-8"))


def _get_svg(base: str, tid: int, year: int, month: int,
             scope: str | None = None) -> str:
    url = f"{base}/api/chart/{tid}.svg?year={year}&month={month}"
    if scope:
        url += f"&scope={scope}"
    return urlreq.urlopen(url, timeout=5).read().decode("utf-8")


def _setup_d2_pair(base: str):
    """建项目 + 维生素D2 L/H 两水平，各录 1 条在控数据；返回 (tid_l, tid_h)。"""
    pid = _post_json(base, "/api/projects", {"name": "免疫组"})["id"]
    tid_l = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": "维生素D2", "level": "L", "lot": "D2-2026A",
        "target": 10.0, "sd": 1.0,
    })["id"]
    tid_h = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": "维生素D2", "level": "H", "lot": "D2-2026A",
        "target": 50.0, "sd": 5.0,
    })["id"]
    for tid, value, level in ((tid_l, 10.0, "L"), (tid_h, 50.0, "H")):
        _post_json(base, f"/api/test_items/{tid}/measurements", {
            "date": "2026-10-14", "seq": 1, "value": value,
            "lot": "D2-2026A", "level": level,
        })
    return tid_l, tid_h


def test_scope_single_L_shows_only_L_curve(web_server):
    """点 L：只有 L 水平一条曲线（含 L 靶值标题，无 H 靶值、无双轴标记）。"""
    base, _ = web_server
    tid_l, _ = _setup_d2_pair(base)
    svg = _get_svg(base, tid_l, 2026, 10, scope="single")
    assert "靶值（L）=10.000" in svg
    assert "靶值（H）=" not in svg
    assert DUAL_MARKER not in svg


def test_scope_single_H_shows_only_H_curve(web_server):
    """点 H：只有 H 水平一条曲线（含 H 靶值标题，无 L 靶值、无双轴标记）。"""
    base, _ = web_server
    _, tid_h = _setup_d2_pair(base)
    svg = _get_svg(base, tid_h, 2026, 10, scope="single")
    assert "靶值（H）=50.000" in svg
    assert "靶值（L）=" not in svg
    assert DUAL_MARKER not in svg


def test_no_scope_keeps_dual_axis(web_server):
    """回归保护：不带 scope 参数仍是 L+H 双轴图（老链接/老测试不破坏）。"""
    base, _ = web_server
    tid_l, _ = _setup_d2_pair(base)
    svg = _get_svg(base, tid_l, 2026, 10)
    assert DUAL_MARKER in svg
    assert "靶值（L）=10.000" in svg
    assert "靶值（H）=50.000" in svg


def test_scope_pair_keeps_dual_axis(web_server):
    """点卡片空白处：scope=pair 仍是 L+H 双轴图。"""
    base, _ = web_server
    tid_l, _ = _setup_d2_pair(base)
    svg = _get_svg(base, tid_l, 2026, 10, scope="pair")
    assert DUAL_MARKER in svg
    assert "靶值（L）=10.000" in svg
    assert "靶值（H）=50.000" in svg


def test_html_scope_js_wiring():
    """静态扫描前端 JS：子行点击置 single、卡片空白点击置 pair、refreshChart 拼 scope。"""
    html = (Path(__file__).parent.parent / "qc_tool" / "web"
            / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'chartScope: "pair"' in html
    assert 'state.chartScope = "single"' in html
    assert 'state.chartScope = "pair"' in html
    assert '&scope=${state.chartScope}' in html
