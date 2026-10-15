# tests/test_web_import_preview.py
"""测试数据导入预览与错误提示诊断功能（测试先行）。"""
import io
import json
import socket
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib import request as urlreq
from urllib.error import HTTPError
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

def _http(method: str, url: str, data=None, raw=None, headers=None) -> tuple[int, object, dict]:
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h = {"Content-Type": "application/json"}
    elif raw is not None:
        body = raw
        h = {"Content-Type": "application/octet-stream"}
    else:
        body = b""
        h = {}
    if headers:
        h.update(headers)
    req = urlreq.Request(url, data=body, method=method, headers=h)
    try:
        resp = urlreq.urlopen(req, timeout=5)
        return resp.status, resp.read(), dict(resp.headers)
    except HTTPError as e:
        return e.code, e.read() if e.read else b"", dict(e.headers or {})

def _seed(base: str) -> dict:
    _, p_raw, _ = _http("POST", f"{base}/api/projects", data={"name": "维生素 D"})
    p = json.loads(p_raw)
    _, ti_l_raw, _ = _http(
        "POST", f"{base}/api/projects/{p['id']}/test_items",
        data={"name": "D2", "level": "L", "lot": "LOT1", "target": 100.0, "sd": 2.0},
    )
    ti_l = json.loads(ti_l_raw)
    return {"pid": p["id"], "tid_l": ti_l["id"]}

def _make_xlsx(rows: list[dict], custom_cols: list[str] = None) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    cols = custom_cols or ["项目名", "检测指标", "水平", "批号", "日期", "测定值", "序号"]
    ws.append(cols)
    for r in rows:
        row_vals = [r.get(c, "") for c in cols]
        ws.append(row_vals)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestWebImportPreview:
    """测试 POST /api/test_items/<tid>/preview_import 预览接口。"""

    def test_01_预览规范数据_返回ok_rows与摘要(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        rows = [
            {"项目名": "维生素 D", "检测指标": "D2", "水平": "L", "批号": "LOT1", "日期": "2026-04-01", "测定值": 100.5, "序号": 1},
            {"项目名": "维生素 D", "检测指标": "D2", "水平": "L", "批号": "LOT1", "日期": "2026-04-02", "测定值": 102.0, "序号": 2},
        ]
        xlsx = _make_xlsx(rows)
        status, body, _ = _http(
            "POST", f"{base}/api/test_items/{ids['tid_l']}/preview_import",
            raw=xlsx,
            headers={"X-Project-Name-Id": str(ids["pid"])},
        )
        assert status == 200, f"预览应返回 200，实际 {status}: {body}"
        res = json.loads(body)
        assert res["can_import"] is True
        assert res["total_count"] == 2
        assert len(res["ok_rows"]) == 2
        assert len(res["skipped_rows"]) == 0
        assert res["ok_rows"][0]["value"] == 100.5
        assert res["ok_rows"][0]["date"] == "2026-04-01"

    def test_02_预览含错误行_返回skipped_rows明细(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        rows = [
            {"项目名": "维生素 D", "检测指标": "D2", "水平": "L", "批号": "LOT1", "日期": "2026-04-01", "测定值": 100.5, "序号": 1},
            {"项目名": "维生素 D", "检测指标": "D2", "水平": "L", "批号": "LOT1", "日期": "invalid-date", "测定值": 102.0, "序号": 2},
            {"项目名": "维生素 D", "检测指标": "D2", "水平": "L", "批号": "LOT1", "日期": "2026-04-03", "测定值": "not-a-number", "序号": 3},
        ]
        xlsx = _make_xlsx(rows)
        status, body, _ = _http(
            "POST", f"{base}/api/test_items/{ids['tid_l']}/preview_import",
            raw=xlsx,
            headers={"X-Project-Name-Id": str(ids["pid"])},
        )
        assert status == 200
        res = json.loads(body)
        assert res["total_count"] == 3
        assert len(res["ok_rows"]) == 1
        assert len(res["skipped_rows"]) == 2
        reasons = [s["reason"] for s in res["skipped_rows"]]
        assert any("日期" in r for r in reasons)
        assert any("测定值" in r for r in reasons)

    def test_03_预览缺列_返回400与明确提示(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        # 缺测定值列
        rows = [{"项目名": "维生素 D", "检测指标": "D2", "水平": "L", "批号": "LOT1", "日期": "2026-04-01"}]
        xlsx = _make_xlsx(rows, custom_cols=["项目名", "检测指标", "水平", "批号", "日期"])
        status, body, _ = _http(
            "POST", f"{base}/api/test_items/{ids['tid_l']}/preview_import",
            raw=xlsx,
            headers={"X-Project-Name-Id": str(ids["pid"])},
        )
        assert status == 400
        err = json.loads(body).get("error", "")
        assert "缺少必需列" in err and "测定值" in err

    def test_04_前端HTML中包含预览弹窗与错误提示元素(self, web_server):
        base, _ = web_server
        status, body, _ = _http("GET", f"{base}/")
        assert status == 200
        html = body.decode("utf-8")
        assert "openImportPreviewModal" in html or "previewImportXlsx" in html
        assert "openImportErrorModal" in html or "showImportError" in html
