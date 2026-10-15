"""
测试：录入面板（右侧）支持「单条录入」与「批量录入」双模式，并支持 Excel 模板上传
- HTML 结构包含 tab-single 与 tab-batch
- HTML 结构包含批量导入上传区（可点击 / 拖拽）与下载模板按钮
- 批量上传支持 .xlsx 格式文件导入并触发 /api/test_items/<tid>/import
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


def test_batch_entry_tab_and_upload_elements_exist(web_server):
    """验证录入面板包含 Tab 切换和批量上传区。"""
    base, _ = web_server
    r = urlreq.urlopen(f"{base}/", timeout=5)
    html = r.read().decode("utf-8")

    # 1. 存在单条与批量录入 Tab
    assert 'id="tab-entry-single"' in html, "缺单条录入 tab"
    assert 'id="tab-entry-batch"' in html, "缺批量录入 tab"
    assert 'switchEntryMode' in html, "缺切换模式 JS 函数 switchEntryMode"

    # 2. 存在批量录入专区结构
    assert 'id="panel-entry-single"' in html, "缺单条录入面板容器"
    assert 'id="panel-entry-batch"' in html, "缺批量录入面板容器"

    # 3. 批量面板包含下载模板与上传触发
    assert 'id="btn-batch-template"' in html, "批量录入面板缺下载模板按钮"
    assert 'id="batch-drop-zone"' in html, "批量录入面板缺拖拽/点击上传区域"
    assert 'id="file-batch-import"' in html, "批量录入面板缺文件选择控件"
    assert 'handleBatchUpload' in html, "缺处理上传函数 handleBatchUpload"
    assert 'initBatchDropZone' in html, "缺初始化拖拽区函数 initBatchDropZone"


def test_batch_entry_upload_integration(web_server):
    """端到端验证：上传真实 xlsx 模板数据完成批量录入并成功入库。"""
    import json
    from qc_tool import excel_io as X

    base, tmp_path = web_server

    # 1. 创建项目
    req = urlreq.Request(
        f"{base}/api/projects",
        data=json.dumps({"name": "生化全套"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    resp = urlreq.urlopen(req, timeout=5)
    proj = json.loads(resp.read().decode("utf-8"))
    pid = proj["id"]

    # 2. 创建指标 ALT
    req = urlreq.Request(
        f"{base}/api/projects/{pid}/test_items",
        data=json.dumps({
            "name": "ALT",
            "lot": "LOT2026",
            "level": "L",
            "target": 40.0,
            "sd": 2.0,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    resp = urlreq.urlopen(req, timeout=5)
    ti = json.loads(resp.read().decode("utf-8"))
    tid = ti["id"]

    # 3. 构造 3 行合法的批量录入 xlsx 字节流
    rows = [
        {"项目名": "生化全套", "检测指标": "ALT", "水平": "L", "批号": "LOT2026", "日期": "2026-04-01", "测定值": 40.2, "序号": 1},
        {"项目名": "生化全套", "检测指标": "ALT", "水平": "L", "批号": "LOT2026", "日期": "2026-04-02", "测定值": 39.8, "序号": 2},
        {"项目名": "生化全套", "检测指标": "ALT", "水平": "L", "批号": "LOT2026", "日期": "2026-04-03", "测定值": 40.5, "序号": 3},
    ]
    xlsx_bytes = X.build_template_xlsx_with_rows(rows)

    # 4. 模拟 POST 上传 Excel
    req = urlreq.Request(
        f"{base}/api/test_items/{tid}/import",
        data=xlsx_bytes,
        headers={
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "X-Project-Name-Id": str(pid),
        },
    )
    resp = urlreq.urlopen(req, timeout=5)
    res = json.loads(resp.read().decode("utf-8"))
    assert res.get("imported_count") == 3
    assert len(res.get("skipped_rows", [])) == 0

    # 5. 查询测量值列表确认 3 条已入库
    resp = urlreq.urlopen(f"{base}/api/test_items/{tid}/measurements", timeout=5)
    measurements = json.loads(resp.read().decode("utf-8"))
    assert len(measurements) == 3
    assert [m["date"] for m in measurements] == ["2026-04-01", "2026-04-02", "2026-04-03"]

