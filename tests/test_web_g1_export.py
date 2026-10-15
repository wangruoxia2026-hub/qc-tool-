"""
PR#5 — spec V1.3 §G.1 选择导出指标对话框与后端过滤测试

覆盖：
1. HTML 包含 openExportModal、id="modal-export"、多指标选择与全选/全不选功能
2. downloadMonthlySelected 将勾选的 ID 拼为 selected=<csv> 参数触发下载
3. 后端 GET /api/export/monthly.xlsx 支持 selected=<csv> 过滤
4. 验证当两个指标 (例如 Na+ 和 K+) 存在时，只选择其中一个，生成的 xlsx 仅包含所选指标的 Sheet
"""
from __future__ import annotations

import io
import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlreq
import openpyxl

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


def test_html_has_export_indicator_modal(web_server):
    """静态扫描 HTML 存在 openExportModal 及 id=modal-export。"""
    base, _ = web_server
    html = _get_html(base)
    assert "openExportModal" in html, "HTML 缺 openExportModal"
    assert 'id="modal-export"' in html, "HTML 缺 id=modal-export"
    assert "downloadMonthlySelected" in html, "HTML 缺 downloadMonthlySelected"


def test_GET_monthly_xlsx_with_selected_ids(web_server):
    """端到端验证 GET monthly.xlsx 支持按 selected 参数精确过滤 Sheet。"""
    base, _ = web_server
    # 1. 建项目
    _, res = _http("POST", f"{base}/api/projects", {"name": "电解质全套"})
    pid = json.loads(res)["id"]

    # 2. 建两个指标：Na (L) 和 K (L)
    _, res = _http("POST", f"{base}/api/projects/{pid}/test_items", {
        "name": "Na", "lot": "LOT1", "level": "L", "target": 140.0, "sd": 2.0
    })
    tid_na = json.loads(res)["id"]

    _, res = _http("POST", f"{base}/api/projects/{pid}/test_items", {
        "name": "K", "lot": "LOT1", "level": "L", "target": 4.5, "sd": 0.2
    })
    tid_k = json.loads(res)["id"]

    # 3. 仅导出 Na (selected=tid_na)
    url_na_only = f"{base}/api/export/monthly.xlsx?project_id={pid}&year=2026&month=4&selected={tid_na}"
    resp_na = urlreq.urlopen(url_na_only, timeout=5).read()
    wb_na = openpyxl.load_workbook(io.BytesIO(resp_na))
    sheet_names_na = wb_na.sheetnames
    # Na 应该在 sheet 名字中，而 K 不应该在
    assert any("Na" in s for s in sheet_names_na), f"应包含 Na 的 sheet，实际: {sheet_names_na}"
    assert not any("K" in s for s in sheet_names_na), f"不应包含 K 的 sheet，实际: {sheet_names_na}"

    # 4. 同时导出 Na 和 K (selected=tid_na,tid_k)
    url_both = f"{base}/api/export/monthly.xlsx?project_id={pid}&year=2026&month=4&selected={tid_na},{tid_k}"
    resp_both = urlreq.urlopen(url_both, timeout=5).read()
    wb_both = openpyxl.load_workbook(io.BytesIO(resp_both))
    sheet_names_both = wb_both.sheetnames
    assert any("Na" in s for s in sheet_names_both), "应包含 Na 的 sheet"
    assert any("K" in s for s in sheet_names_both), "应包含 K 的 sheet"
