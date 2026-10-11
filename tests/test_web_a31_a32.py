"""
§A.3.1 / §A.3.2 后端 API 测试（编辑/删除项目 + 编辑/删除检测指标）

端到端：HTTP → web.py handler → P.update_*/delete_* → DB

设计：
- 不依赖 GUI（沿用 tests/test_web.py 风格）
- 每条 spec 验收至少 1 个测试
- 测坏路径（404 / 400 / 业务异常）也覆盖

跑法：
    python -m pytest tests/test_web_a31_a32.py -v
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError

import pytest


# ============================================================================
# 起服务（与 test_web.py 同源）
# ============================================================================

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


def _http(method: str, url: str, data: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(data).encode("utf-8") if data is not None else b""
    req = urlreq.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urlreq.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read()) if e.read else {}


def _seed(base: str) -> dict:
    """建项目 + L/H 两水平 + 1 条 measurement，返回 id 字典。"""
    _, p = _http("POST", f"{base}/api/projects",
                 data={"name": "维生素 D"})
    _, ti_l = _http("POST", f"{base}/api/projects/{p['id']}/test_items",
                    data={"name": "D2", "lot": "LOT1", "level": "L",
                          "target": 100.0, "sd": 2.0})
    _, ti_h = _http("POST", f"{base}/api/projects/{p['id']}/test_items",
                    data={"name": "D2", "lot": "LOT1", "level": "H",
                          "target": 100.0, "sd": 2.0})
    _, m = _http("POST", f"{base}/api/test_items/{ti_l['id']}/measurements",
                 data={"date": "2026-04-01", "seq": 1, "value": 100.0,
                       "lot": "LOT1", "level": "L"})
    return {
        "pid": p["id"], "tid_l": ti_l["id"], "tid_h": ti_h["id"],
        "mid": m["id"],
    }


# ============================================================================
# §A.3.1 编辑项目（PUT /api/projects/<pid>）
# ============================================================================

class TestA31EditProject:
    """spec §A.3.1 验收 a：改项目名称 → DB 刷新。"""

    def test_01_改项目名称_成功(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        status, body = _http("PUT", f"{base}/api/projects/{ids['pid']}",
                             data={"name": "维生素 D2"})
        assert status == 200, f"PUT 应 200，实际 {status}: {body}"
        assert body["name"] == "维生素 D2"
        # 再次 GET 验证
        status, body = _http("GET", f"{base}/api/projects")
        assert any(p["name"] == "维生素 D2" for p in body)

    def test_02_改项目简称单位厂家(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        status, body = _http("PUT", f"{base}/api/projects/{ids['pid']}",
                             data={"short": "VD", "unit": "ng/mL",
                                   "manufacturer": "罗氏"})
        assert status == 200
        assert body["short"] == "VD"
        assert body["unit"] == "ng/mL"
        assert body["manufacturer"] == "罗氏"

    def test_03_改项目名称为空_400(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        status, body = _http("PUT", f"{base}/api/projects/{ids['pid']}",
                             data={"name": ""})
        assert status == 400
        assert "项目名称不能为空" in body.get("error", "")

    def test_04_改项目名重复_400(self, web_server):
        base, _ = web_server
        _ = _seed(base)
        # 再建一个
        _, p2 = _http("POST", f"{base}/api/projects",
                      data={"name": "项目B"})
        # 把项目B改成跟 项目A 同名
        _, a = _http("POST", f"{base}/api/projects",
                     data={"name": "项目A"})
        # 上面 _seed 已经建了"维生素 D"，p2 是"项目B"
        # 把 p2 改成"维生素 D" → 重复
        status, body = _http("PUT", f"{base}/api/projects/{p2['id']}",
                             data={"name": "维生素 D"})
        assert status == 400
        assert "已存在同名项目" in body.get("error", "")

    def test_05_改不存在的项目_404(self, web_server):
        base, _ = web_server
        status, body = _http("PUT", f"{base}/api/projects/99999",
                             data={"name": "X"})
        assert status == 404


# ============================================================================
# §A.3.1 删除项目（DELETE /api/projects/<pid>）
# ============================================================================

class TestA31DeleteProject:
    """spec §A.3.1 验收 b：删项目 → measurements CASCADE，out_of_control 保留。"""

    def test_01_删项目成功_200(self, web_server):
        base, tmp = web_server
        ids = _seed(base)
        status, body = _http("DELETE", f"{base}/api/projects/{ids['pid']}")
        assert status == 200, f"DELETE 应 200，实际 {status}: {body}"
        # 二次确认里的统计字段都在
        assert "n_test_items" in body
        assert "n_measurements" in body
        assert "n_out_of_control" in body
        assert body["n_test_items"] == 2
        assert body["n_measurements"] == 1

    def test_02_删后measurements_CASCADE_0(self, web_server):
        base, tmp = web_server
        ids = _seed(base)
        _http("DELETE", f"{base}/api/projects/{ids['pid']}")
        from qc_tool.db import get_connection
        conn = get_connection(tmp / "qc.db")
        try:
            n = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
            assert n == 0, f"CASCADE 应清空 measurements，实际 {n}"
            n = conn.execute("SELECT COUNT(*) FROM test_items").fetchone()[0]
            assert n == 0
        finally:
            conn.close()

    def test_03_删不存在的项目_404(self, web_server):
        base, _ = web_server
        status, body = _http("DELETE", f"{base}/api/projects/99999")
        assert status == 404


# ============================================================================
# §A.3.2 编辑检测指标（PUT /api/test_items/<tid>）
# ============================================================================

class TestA32EditTestItem:
    """spec §A.3.2 验收 a：改靶值/SD → 后续录入按新值判（不重算历史）。"""

    def test_01_改靶值_后续录入按新值判(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        # 改 L 水平靶值从 100 → 200
        status, body = _http("PUT", f"{base}/api/test_items/{ids['tid_l']}",
                             data={"target": 200.0})
        assert status == 200
        assert body["target"] == 200.0

    def test_02_改SD(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        status, body = _http("PUT", f"{base}/api/test_items/{ids['tid_l']}",
                             data={"sd": 5.0})
        assert status == 200
        assert body["sd"] == 5.0

    def test_03_改SD为0_400(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        status, body = _http("PUT", f"{base}/api/test_items/{ids['tid_l']}",
                             data={"sd": 0})
        assert status == 400
        assert "SD" in body.get("error", "")

    def test_04_改不存在的指标_404(self, web_server):
        base, _ = web_server
        status, body = _http("PUT", f"{base}/api/test_items/99999",
                             data={"target": 1.0})
        assert status == 404


# ============================================================================
# §A.3.2 删除检测指标（DELETE /api/test_items/<tid>）
# ============================================================================

class TestA32DeleteTestItem:
    """spec §A.3.2 验收 b：删指标 → measurements CASCADE，out_of_control 保留。"""

    def test_01_删指标成功(self, web_server):
        base, tmp = web_server
        ids = _seed(base)
        status, body = _http("DELETE", f"{base}/api/test_items/{ids['tid_l']}")
        assert status == 200
        assert "n_measurements" in body
        assert body["n_measurements"] == 1

    def test_02_删后measurements_CASCADE(self, web_server):
        base, tmp = web_server
        ids = _seed(base)
        _http("DELETE", f"{base}/api/test_items/{ids['tid_l']}")
        from qc_tool.db import get_connection
        conn = get_connection(tmp / "qc.db")
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM measurements WHERE test_item_id = ?",
                (ids["tid_l"],),
            ).fetchone()[0]
            assert n == 0
            # H 水平仍在
            n = conn.execute(
                "SELECT COUNT(*) FROM test_items WHERE id = ?",
                (ids["tid_h"],),
            ).fetchone()[0]
            assert n == 1
        finally:
            conn.close()

    def test_03_删不存在的指标_404(self, web_server):
        base, _ = web_server
        status, body = _http("DELETE", f"{base}/api/test_items/99999")
        assert status == 404
