"""
PR#1 测试：spec §B.2 + §B.3 前端入口

覆盖：
- HTML 含"下载模板" / "导入 Excel" / 隐藏 file input
- HTML JS 含 downloadTemplate / onImportFileSelected / 调 /api/template.xlsx
- HTML 弹"全量预览结果"modal（spec V1.3 §B.2 用户拍板 full-preview）
- 端到端：上传 50 行规范数据 → imported_count==50（PR#1 回归用，逻辑同 test_web_a4_b2_b3.py）
- 端到端：上传 1 行日期错 → 弹窗 skipped_rows 含 "日期"

跑法：
    python -m pytest tests/test_web_b2_b3_buttons.py -v
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

import pytest


# ============================================================================
# Fixtures（与 test_web_a4_b2_b3.py 同源）
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


def _get_html(base: str) -> str:
    return urlreq.urlopen(f"{base}/", timeout=5).read().decode("utf-8")


def _seed(base: str) -> dict:
    """建项目 + D2-L 指标 + 实验室名称。"""
    _, p, _ = _http3("POST", f"{base}/api/projects", data={"name": "维生素 D"})
    _, lab, _ = _http3("POST", f"{base}/api/lab", data={"lab_name": "测试实验室"})
    _, ti, _ = _http3(
        "POST", f"{base}/api/projects/{p['id']}/test_items",
        data={"name": "D2", "lot": "LOT1", "level": "L",
              "target": 100.0, "sd": 2.0},
    )
    return {"pid": p["id"], "tid_l": ti["id"]}


def _http3(method: str, url: str, data=None) -> tuple[int, object, dict]:
    """3-tuple：(status, parsed_body_or_raw, headers)。"""
    body = json.dumps(data).encode("utf-8") if data is not None else b""
    req = urlreq.Request(url, data=body, method=method,
                         headers={"Content-Type": "application/json"})
    try:
        resp = urlreq.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read()), dict(resp.headers)
    except Exception as e:
        if hasattr(e, "code"):
            try:
                return e.code, json.loads(e.read()), {}
            except Exception:
                return e.code, e.read(), {}
        raise


def _http_raw(method: str, url: str, raw: bytes,
              headers: dict) -> tuple[int, object, dict]:
    """上传 xlsx 用的 raw 版本。"""
    h = {"Content-Type": "application/octet-stream"}
    h.update(headers)
    req = urlreq.Request(url, data=raw, method=method, headers=h)
    try:
        resp = urlreq.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read()), dict(resp.headers)
    except Exception as e:
        if hasattr(e, "code"):
            try:
                return e.code, json.loads(e.read()), {}
            except Exception:
                return e.code, e.read(), {}
        raise


# ============================================================================
# 静态扫描：HTML 含 §B.2 / §B.3 入口元素
# ============================================================================

class TestHtmlB2B3Buttons:
    """PR#1 前端入口元素 + JS 函数存在性。"""

    def test_01_HTML_含下载模板按钮(self, web_server):
        base, _ = web_server
        html = _get_html(base)
        assert 'id="btn-template"' in html, \
            "HTML 缺 '下载模板' 按钮（id=btn-template）"
        assert "下载模板" in html, "HTML 缺 '下载模板' 文案"

    def test_02_HTML_含导入Excel按钮_and_隐藏file_input(self, web_server):
        base, _ = web_server
        html = _get_html(base)
        assert 'id="btn-import"' in html, "HTML 缺 '导入 Excel' 按钮（id=btn-import）"
        assert 'id="file-import"' in html, "HTML 缺隐藏 file input（id=file-import）"
        assert 'accept=".xlsx"' in html, "file-import 应限定 .xlsx 后缀"
        assert "导入 Excel" in html, "HTML 缺 '导入 Excel' 文案"

    def test_03_JS_downloadTemplate_调_api_template_xlsx(self, web_server):
        base, _ = web_server
        html = _get_html(base)
        idx = html.find("async function downloadTemplate")
        assert idx > 0, "downloadTemplate 函数未找到"
        chunk = html[idx:idx + 1200]
        assert "/api/template.xlsx" in chunk, \
            "downloadTemplate 体内没调 /api/template.xlsx"
        assert "qc_template.xlsx" in chunk, \
            "downloadTemplate 体内没设置下载文件名 qc_template.xlsx"

    def test_04_JS_onImportFileSelected_挂file_input_change(self, web_server):
        base, _ = web_server
        html = _get_html(base)
        # init 体内挂监听
        idx = html.find("async function init()")
        assert idx > 0, "init 函数未找到"
        chunk = html[idx:idx + 1500]
        assert "file-import" in chunk, "init 体内没引用 file-import"
        assert "onImportFileSelected" in chunk, "init 体内没挂 onImportFileSelected"
        # 函数本身存在
        assert "async function onImportFileSelected" in html, \
            "onImportFileSelected 函数未定义"

    def test_05_JS_importXlsxNow_调_POST_import_with_FormData(self, web_server):
        base, _ = web_server
        html = _get_html(base)
        idx = html.find("async function importXlsxNow")
        assert idx > 0, "importXlsxNow 函数未找到"
        chunk = html[idx:idx + 1500]
        assert "FormData" in chunk, "importXlsxNow 没用 FormData"
        assert "/api/test_items/" in chunk, "importXlsxNow 没调 /api/test_items/.../import"
        assert "X-Project-Name-Id" in chunk, \
            "importXlsxNow 没传 X-Project-Name-Id header（项目 ID 必传）"

    def test_06_JS_showImportResultModal_弹全量预览结果(self, web_server):
        """spec §B.2 用户拍板 full-preview：必须弹结果窗显示 imported + skipped 详情。"""
        base, _ = web_server
        html = _get_html(base)
        assert "function showImportResultModal" in html, \
            "showImportResultModal 函数未定义（spec §B.2 全量预览）"
        # 必须显示 imported_count + skipped_rows
        idx = html.find("function showImportResultModal")
        chunk = html[idx:idx + 1500]
        assert "imported_count" in chunk, "showImportResultModal 没显示 imported_count"
        assert "skipped_rows" in chunk, "showImportResultModal 没显示 skipped_rows"


# ============================================================================
# 端到端：HTTP 行为（与现有 §B.2 后端测试对齐，PR#1 回归保证）
# ============================================================================

class TestE2EImportE2E:
    """PR#1 端到端：覆盖 HTML 弹窗依赖的后端响应结构。"""

    def test_07_上传50行规范数据_imported_count_eq_50(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["项目名", "检测指标", "水平", "批号", "日期", "测定值", "序号"])
        for i in range(50):
            ws.append(["维生素 D", "D2", "L", "LOT1",
                       f"2026-04-{(i % 28) + 1:02d}", 100.0, i % 5])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx = buf.getvalue()
        status, body, _ = _http_raw(
            "POST", f"{base}/api/test_items/{ids['tid_l']}/import",
            raw=xlsx,
            headers={"X-Project-Name-Id": str(ids["pid"])},
        )
        assert status == 200, f"导入应 200，实际 {status}: {body}"
        assert body["imported_count"] == 50, \
            f"应入 50 行，实际 {body.get('imported_count')}"

    def test_08_上传1行日期错_skipped_rows_含日期原因(self, web_server):
        base, _ = web_server
        ids = _seed(base)
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["项目名", "检测指标", "水平", "批号", "日期", "测定值", "序号"])
        ws.append(["维生素 D", "D2", "L", "LOT1", "2026-04-01", 100.0, 1])
        ws.append(["维生素 D", "D2", "L", "LOT1", "不是日期", 100.0, 2])  # 错
        ws.append(["维生素 D", "D2", "L", "LOT1", "2026-04-03", 100.0, 3])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx = buf.getvalue()
        status, body, _ = _http_raw(
            "POST", f"{base}/api/test_items/{ids['tid_l']}/import",
            raw=xlsx,
            headers={"X-Project-Name-Id": str(ids["pid"])},
        )
        assert status == 200
        assert body["imported_count"] == 2
        assert len(body["skipped_rows"]) == 1
        # 前端 showImportResultModal 需要 original_row_no + reason 字段
        assert "original_row_no" in body["skipped_rows"][0]
        assert "reason" in body["skipped_rows"][0]
        assert "日期" in body["skipped_rows"][0]["reason"], \
            f"跳过原因应含 '日期'，实际 {body['skipped_rows'][0]['reason']!r}"

    def test_09_下载模板_GET_template_xlsx(self, web_server):
        """前端 downloadTemplate 调用的后端端点。"""
        base, _ = web_server
        req = urlreq.Request(f"{base}/api/template.xlsx")
        resp = urlreq.urlopen(req, timeout=5)
        assert resp.status == 200
        body = resp.read()
        # xlsx 是 zip 格式
        assert body[:4] == b"PK\x03\x04"
        # 验证 Content-Disposition 利于前端 downloadTemplate 提取文件名
        cd = resp.headers.get("Content-Disposition", "")
        assert "qc_template.xlsx" in cd, \
            f"Content-Disposition 应含 qc_template.xlsx，实际 {cd!r}"
