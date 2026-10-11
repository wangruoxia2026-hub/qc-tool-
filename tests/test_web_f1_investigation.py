"""
Step 1+2 P1 partial: §F.1 失控排查完整版

端到端：
- 录一条触发 1_3s 的 measurement
- POST /api/ooc/<mid>/investigate 写 checklist + conclusion
- 验证 out_of_control.checklist 字段已落库（JSON）

跑法：
    python -m pytest tests/test_web_f1_investigation.py -v
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


def _http(method: str, url: str, data=None) -> tuple[int, dict]:
    body = json.dumps(data).encode("utf-8") if data is not None else b""
    req = urlreq.Request(url, data=body, method=method,
                         headers={"Content-Type": "application/json"})
    try:
        resp = urlreq.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except Exception as e:
        if hasattr(e, "code"):
            return e.code, json.loads(e.read()) if e.read else {}
        raise


def _http3(method: str, url: str, data=None) -> tuple[int, object, dict]:
    """3-tuple 版本：_http + headers。"""
    body = json.dumps(data).encode("utf-8") if data is not None else b""
    req = urlreq.Request(url, data=body, method=method,
                         headers={"Content-Type": "application/json"})
    try:
        resp = urlreq.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read()), dict(resp.headers)
    except Exception as e:
        if hasattr(e, "code"):
            return e.code, json.loads(e.read()) if e.read else {}, {}
        raise


def test_post_investigation_saves_checklist(web_server):
    """§F.1 验收：排查 checklist 写库成功。"""
    base, tmp = web_server
    # 建项目 + 指标 + 1 条触发 1_3s 的 measurement
    _, p, _ = _http3("POST", f"{base}/api/projects", {"name": "P1"})
    _, ti, _ = _http3("POST", f"{base}/api/projects/{p['id']}/test_items",
                     {"name": "T", "lot": "L1", "level": "L",
                      "target": 5.0, "sd": 0.1})
    _, m, _ = _http3("POST", f"{base}/api/test_items/{ti['id']}/measurements",
                    {"date": "2026-04-01", "seq": 1, "value": 6.0,
                     "lot": "L1", "level": "L"})
    assert m["status"] in ("OutOfControl", "失控") or "失控" in m["status"]
    # 排查写库
    status, body = _http(
        "POST", f"{base}/api/ooc/{m['id']}/investigate",
        data={"items": [
            {"category": "随机误差", "item": "重测控制品"},
            {"category": "随机误差", "item": "检查吸样/加样误差"},
        ], "conclusion": "重测后正常"},
    )
    assert status == 200, f"应 200，实际 {status}: {body}"
    # 验证 DB
    from qc_tool.db import get_connection
    conn = get_connection(tmp / "qc.db")
    try:
        row = conn.execute(
            "SELECT checklist, conclusion FROM out_of_control "
            "WHERE measurement_id = ?", (m["id"],),
        ).fetchone()
        assert row is not None, "out_of_control 缺记录"
        cl = json.loads(row["checklist"])
        assert "随机误差" in cl
        assert "重测控制品" in cl["随机误差"]
        assert row["conclusion"] == "重测后正常"
    finally:
        conn.close()


def test_post_investigation_empty_items_400(web_server):
    """§F.1 d 验收：checklist 不能为空。"""
    base, tmp = web_server
    _, p, _ = _http3("POST", f"{base}/api/projects", {"name": "P1"})
    _, ti, _ = _http3("POST", f"{base}/api/projects/{p['id']}/test_items",
                     {"name": "T", "lot": "L1", "level": "L",
                      "target": 5.0, "sd": 0.1})
    _, m, _ = _http3("POST", f"{base}/api/test_items/{ti['id']}/measurements",
                    {"date": "2026-04-01", "seq": 1, "value": 6.0,
                     "lot": "L1", "level": "L"})
    status, body = _http(
        "POST", f"{base}/api/ooc/{m['id']}/investigate",
        data={"items": []},
    )
    assert status == 400


def test_post_investigation_404_nonexistent_mid(web_server):
    base, _ = web_server
    status, body = _http(
        "POST", f"{base}/api/ooc/99999/investigate",
        data={"items": [{"category": "其他", "item": "X"}]},
    )
    assert status == 404
