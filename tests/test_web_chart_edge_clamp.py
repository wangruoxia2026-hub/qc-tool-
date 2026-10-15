"""
自动化测试：Web 质控图【超限点钉边显示 + spec §E.1 点样式修正】
（2026-10-12 用户确认的三条规则：
  1. 超出 ±4SD 的点不丢弃：钉在图区上/下边缘并标注实际值，悬停保留真实数据；
  2. 失控=红色实心圆、警告=黄色三角、在控=黑色圆点；
  3. 作废点图上不显示（spec §E.1）。）
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


def _post_json(base: str, path: str, payload: dict) -> dict:
    req = urlreq.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urlreq.urlopen(req, timeout=5).read().decode("utf-8"))


def _get_svg(base: str, tid: int, year: int, month: int) -> str:
    return urlreq.urlopen(
        f"{base}/api/chart/{tid}.svg?year={year}&month={month}", timeout=5
    ).read().decode("utf-8")


def _make_item(base: str, *, name: str, level: str, target: float, sd: float) -> int:
    pid = _post_json(base, "/api/projects", {"name": f"P-{name}-{level}"})["id"]
    return _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": name, "level": level, "lot": "LOT",
        "target": target, "sd": sd,
    })["id"]


def test_beyond_4sd_point_pinned_with_annotation(web_server):
    """超出 ±4SD 的失控点必须出现在图上（钉边 + 标注实际值）。"""
    base, _ = web_server
    tid = _make_item(base, name="Glu", level="L", target=50.0, sd=5.0)

    # 四种状态各一条：在控 52 / 警告 62(+2.4SD) / 失控上 85(+7SD) / 失控下 25(-5SD)
    _post_json(base, f"/api/test_items/{tid}/measurements",
               {"date": "2026-04-01", "seq": 1, "value": 52.0, "lot": "LOT", "level": "L"})
    _post_json(base, f"/api/test_items/{tid}/measurements",
               {"date": "2026-04-02", "seq": 1, "value": 62.0, "lot": "LOT", "level": "L"})
    _post_json(base, f"/api/test_items/{tid}/measurements",
               {"date": "2026-04-03", "seq": 1, "value": 85.0, "lot": "LOT", "level": "L"})
    _post_json(base, f"/api/test_items/{tid}/measurements",
               {"date": "2026-04-04", "seq": 1, "value": 25.0, "lot": "LOT", "level": "L"})

    svg = _get_svg(base, tid, 2026, 4)

    # 1. 超限点钉边 + 标注实际值（不丢弃）
    assert "↑85.000" in svg, "超出 +4SD 的点应钉在上边缘并标注 85.000"
    assert "↓25.000" in svg, "超出 -4SD 的点应钉在下边缘并标注 25.000"
    # 2. 悬停 tooltip 保留真实数据
    assert "85.000" in svg and "25.000" in svg
    # 3. spec §E.1 点样式：在控黑圆 / 警告黄三角 / 失控红实心圆
    assert 'fill="black"' in svg, "在控点应为黑色圆点"
    assert 'fill="#d9a400"' in svg, "警告点应为黄色三角"
    assert 'fill="#c0392b"' in svg, "失控点应为红色实心圆"
    # 4. 图例不再包含"作废"（spec §E.1：作废点不显示）
    assert "作废" not in svg


def test_void_point_not_drawn_on_chart(web_server):
    """作废点在质控图上不显示（spec §E.1）。"""
    base, _ = web_server
    tid = _make_item(base, name="Glu", level="L", target=50.0, sd=5.0)

    m_void = _post_json(base, f"/api/test_items/{tid}/measurements",
                        {"date": "2026-04-01", "seq": 1, "value": 51.0, "lot": "LOT", "level": "L"})
    _post_json(base, f"/api/test_items/{tid}/measurements",
               {"date": "2026-04-02", "seq": 1, "value": 52.0, "lot": "LOT", "level": "L"})

    # DELETE = 标记作废（§B.4 d，幂等）
    req = urlreq.Request(f"{base}/api/measurements/{m_void['id']}", method="DELETE")
    urlreq.urlopen(req, timeout=5)

    svg = _get_svg(base, tid, 2026, 4)
    assert "51.000" not in svg, "作废点的值不应出现在质控图上"
    assert "52.000" in svg, "在控点应正常显示"


def test_dual_axis_chart_keeps_both_levels(web_server):
    """双轴图：两水平 ±5SD 失控点钉边 + 标注实际值（跨度封顶 ±4SD），作废图例不出现。"""
    base, _ = web_server
    pid = _post_json(base, "/api/projects", {"name": "P-pair"})["id"]
    tid_l = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": "Glu", "level": "L", "lot": "LOT", "target": 10.0, "sd": 1.0,
    })["id"]
    tid_h = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": "Glu", "level": "H", "lot": "LOT", "target": 50.0, "sd": 5.0,
    })["id"]

    _post_json(base, f"/api/test_items/{tid_l}/measurements",
               {"date": "2026-04-01", "seq": 1, "value": 15.0, "lot": "LOT", "level": "L"})  # +5SD 失控
    _post_json(base, f"/api/test_items/{tid_h}/measurements",
               {"date": "2026-04-01", "seq": 1, "value": 25.0, "lot": "LOT", "level": "H"})  # -5SD 失控

    svg = _get_svg(base, tid_l, 2026, 4)
    assert "[L水平]" in svg and "[H水平]" in svg
    # 跨度封顶 ±4SD：±5SD 点钉边 + 标注实际值（与单图行为一致）
    assert "↑15.000" in svg, "L 侧 +5SD 点应钉在上边缘并标注"
    assert "↓25.000" in svg, "H 侧 -5SD 点应钉在下边缘并标注"
    # L 失控红圆 / H 失控红三角（各 1 点 + 图例 1）
    assert len(re.findall(r'<circle[^>]*fill="#c0392b"', svg)) == 2
    assert len(re.findall(r'<polygon[^>]*fill="#c0392b"', svg)) == 2
    assert "作废" not in svg
