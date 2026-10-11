"""
Step 0 验收测试 —— spec V1.3 §B.1 录入表单透传 operator / pre_processor

Step 0 范围：
- 仅测试"录入表单 → 业务层 → DB" 的字段透传，不测 GUI 控件
- 业务模块 0 修改（measurements.create_measurement 已支持 operator / pre_processor kwargs）
- 端到端路径：HTTP POST → web.py handler → M.create_measurement → DB 列

设计要点：
- 启一个真 web server（参考 tests/test_web.py 的 web_server fixture）
- 模拟前端录入表单提交：data 含 date/seq/value/lot/level/operator/pre_processor
- 断言 DB 中 measurements.operator / pre_processor 列落库正确

跑法：
    python -m pytest tests/test_web_entryform_operator.py -v
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

import pytest


# ============================================================================
# 起服务（拷贝 tests/test_web.py 的最小版）
# ============================================================================

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch):
    """起 web 服务器，yield (base_url, db_path)。"""
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
    except Exception as e:
        if hasattr(e, "code"):
            return e.code, json.loads(e.read()) if e.read else {}
        raise


def _setup_project_and_ti(base: str, *, project: str = "维生素 D") -> int:
    """建项目 + 1 个 D2 指标，返回 test_item_id。"""
    _, p = _http("POST", f"{base}/api/projects", data={"name": project})
    pid = p["id"]
    _, ti = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "D2", "lot": "LOT1", "level": "L",
              "target": 100.0, "sd": 2.0},
    )
    return ti["id"]


def _read_measurement_row(db_path: Path, mid: int) -> sqlite3.Row:
    from qc_tool.db import get_connection
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT operator, pre_processor, lot, level, value, status "
            "FROM measurements WHERE id = ?", (mid,),
        ).fetchone()
    finally:
        conn.close()


# ============================================================================
# §B.1 录入表单：操作人员 / 前处理人员 端到端测试
# ============================================================================

class TestEntryFormOperatorFields:
    """spec §0 名词表（操作人员 / 前处理人员）+ §B.1 录入表单。

    端到端：HTTP POST /api/test_items/<tid>/measurements → DB。
    """

    def test_01_录入带operator和pre_processor_DB落库(self, web_server):
        """验收 a：表单填操作人员"张三"、前处理人员"李四" → DB 落库正确。"""
        base, tmp = web_server
        tid = _setup_project_and_ti(base)
        status, m = _http(
            "POST", f"{base}/api/test_items/{tid}/measurements",
            data={"date": "2026-04-01", "seq": 1, "value": 100.0,
                  "lot": "LOT1", "level": "L",
                  "operator": "张三", "pre_processor": "李四"},
        )
        assert status == 200, f"POST 应成功，实际 {status}: {m}"
        row = _read_measurement_row(tmp / "qc.db", m["id"])
        assert row["operator"] == "张三", f"operator 落库错：{row['operator']!r}"
        assert row["pre_processor"] == "李四", \
            f"pre_processor 落库错：{row['pre_processor']!r}"

    def test_02_录入不填operator和pre_processor_DB为NULL(self, web_server):
        """验收 c：不输也能保存（spec §0 没强制必填）→ operator=NULL。"""
        base, tmp = web_server
        tid = _setup_project_and_ti(base)
        status, m = _http(
            "POST", f"{base}/api/test_items/{tid}/measurements",
            data={"date": "2026-04-01", "seq": 1, "value": 100.0,
                  "lot": "LOT1", "level": "L"},
            # 不带 operator / pre_processor
        )
        assert status == 200
        row = _read_measurement_row(tmp / "qc.db", m["id"])
        assert row["operator"] is None
        assert row["pre_processor"] is None

    def test_03_只填operator_pre_processor为NULL(self, web_server):
        """边界：只填一个，另一个留空 → 入库 NULL。"""
        base, tmp = web_server
        tid = _setup_project_and_ti(base)
        status, m = _http(
            "POST", f"{base}/api/test_items/{tid}/measurements",
            data={"date": "2026-04-01", "seq": 1, "value": 100.0,
                  "lot": "LOT1", "level": "L",
                  "operator": "王五"},
        )
        assert status == 200
        row = _read_measurement_row(tmp / "qc.db", m["id"])
        assert row["operator"] == "王五"
        assert row["pre_processor"] is None

    def test_04_operator入库不影响Westgard判定(self, web_server):
        """回归：人员字段不能影响 Westgard 状态列。"""
        base, tmp = web_server
        tid = _setup_project_and_ti(base)
        # 104.1 → +2.05SD → 警告(1_2s)
        status, m = _http(
            "POST", f"{base}/api/test_items/{tid}/measurements",
            data={"date": "2026-04-01", "seq": 1, "value": 104.1,
                  "lot": "LOT1", "level": "L",
                  "operator": "张三", "pre_processor": "李四"},
        )
        assert status == 200
        assert m["status"] in ("警告", "Warning") or m["status"].startswith("警告"), \
            f"判定应警告，实际 {m['status']!r}"
        # 也要在 DB 看到落库
        row = _read_measurement_row(tmp / "qc.db", m["id"])
        assert row["operator"] == "张三"
        assert row["pre_processor"] == "李四"
