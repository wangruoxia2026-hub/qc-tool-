"""测定值三位小数量化自动化验收（测试先行）。

需求：Excel 导入的测定值自动保留三位小数入库；界面各处展示固定三位小数。
对应验收：
  A. 解析阶段 ImportRow.value 已量化三位小数
  B. 入库后 measurements.value 为三位小数（12.3456789 → 12.346；40 → 40.0，显示 40.000）
  C. 导入预览接口返回已量化的三位小数值
  D. 前端测定值展示（导入预览表 / 图表点位面板 / 明细列表 / 编辑弹窗）统一走 fmt3 三位格式化
"""
from __future__ import annotations

import io
import json
import socket
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlreq

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import excel_io as X
from qc_tool import projects as P


# ============================================================================
# 工具
# ============================================================================

def _fmt3(v) -> str:
    """与前端 fmt3 等价的 Python 侧格式化（用于断言展示文案）。"""
    if v is None or v == "":
        return "—"
    return f"{float(v):.3f}"


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "qc.db"
    init_database(db)
    c = get_connection(db)
    c.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, NULL)")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _seed(conn: sqlite3.Connection) -> dict:
    P.set_lab_name(conn, "测试检验科")
    proj = P.create_project(conn, name="生化五项")
    ti_l = P.create_test_item(
        conn, project_id=proj.id, name="ALT", level="L",
        lot="LOT2026A", target=10.0, sd=1.0,
    )
    ti_h = P.create_test_item(
        conn, project_id=proj.id, name="ALT", level="H",
        lot="LOT2026A", target=50.0, sd=3.0,
    )
    return {"project_id": proj.id, "ti_l": ti_l.id, "ti_h": ti_h.id}


def _make_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    cols = ["项目名", "检测指标", "水平", "批号", "日期", "测定值", "序号"]
    ws.append(cols)
    for r in rows:
        ws.append([r.get(c, "") for c in cols])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _http(method: str, url: str, data=None, raw=None, headers=None):
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
    with urlreq.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


# ============================================================================
# A / B：解析与入库三位小数
# ============================================================================

class TestImportValueQuantized:
    """导入的测定值必须自动量化为三位小数。"""

    def test_01_多位小数_解析阶段已量化为三位(self, conn):
        rows = [{
            "项目名": "生化五项", "检测指标": "ALT", "水平": "L",
            "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.123456, "序号": 1,
        }]
        plan = X.parse_import_xlsx_bytes(_make_xlsx(rows))
        assert len(plan.ok_rows) == 1
        v = float(plan.ok_rows[0].value)
        assert v == pytest.approx(10.123)
        assert round(v, 3) == v, f"解析后应为三位小数，实际 {v}"

    def test_02_多位小数_入库后为三位小数(self, conn):
        meta = _seed(conn)
        rows = [
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L",
             "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.123456, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "H",
             "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 50.987654, "序号": 1},
        ]
        plan = X.parse_import_xlsx_bytes(_make_xlsx(rows))
        X.import_rows(conn, plan, project_resolver=lambda n: meta["project_id"],
                      skip_duplicates=True)
        got = {r["level"]: r["value"] for r in conn.execute(
            "SELECT level, value FROM measurements").fetchall()}
        assert got["L"] == pytest.approx(10.123)
        assert got["H"] == pytest.approx(50.988)
        assert round(float(got["L"]), 3) == float(got["L"])
        assert round(float(got["H"]), 3) == float(got["H"])

    def test_03_整数值_入库为整值且展示为40点000(self, conn):
        """用户指定验收：导入 40 → 库中为 40.0，界面展示 40.000。"""
        meta = _seed(conn)
        rows = [{
            "项目名": "生化五项", "检测指标": "ALT", "水平": "L",
            "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 40, "序号": 1,
        }]
        plan = X.parse_import_xlsx_bytes(_make_xlsx(rows))
        X.import_rows(conn, plan, project_resolver=lambda n: meta["project_id"],
                      skip_duplicates=True)
        row = conn.execute("SELECT value FROM measurements").fetchone()
        v = float(row["value"])
        assert v == 40.0
        assert _fmt3(v) == "40.000", "界面应展示三位小数 40.000"

    def test_04_字符串数值_同样量化(self, conn):
        rows = [{
            "项目名": "生化五项", "检测指标": "ALT", "水平": "L",
            "批号": "LOT2026A", "日期": "2026-10-01", "测定值": "10.5555", "序号": 1,
        }]
        plan = X.parse_import_xlsx_bytes(_make_xlsx(rows))
        assert len(plan.ok_rows) == 1
        assert float(plan.ok_rows[0].value) == pytest.approx(10.556)

    def test_05_量化不影响既有的非数字跳过逻辑(self, conn):
        rows = [
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L",
             "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.123456, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "H",
             "批号": "LOT2026A", "日期": "2026-10-01", "测定值": "abc", "序号": 1},
        ]
        plan = X.parse_import_xlsx_bytes(_make_xlsx(rows))
        assert len(plan.ok_rows) == 1
        assert len(plan.skipped_rows) == 1
        assert "测定值" in plan.skipped_rows[0].reason


# ============================================================================
# C：预览接口
# ============================================================================

class TestPreviewApiReturnsQuantized:
    """导入预览接口返回的测定值必须是三位小数。"""

    def test_06_预览返回三位小数值(self, web_server):
        base, _ = web_server
        _, p = _http("POST", f"{base}/api/projects",
                     data={"name": "导入三位测试", "short": "T", "manufacturer": "R"})
        _, ti = _http("POST", f"{base}/api/projects/{p['id']}/test_items",
                      data={"name": "ALT", "level": "L", "lot": "LOT1",
                            "target": 10.0, "sd": 1.0})
        rows = [
            {"项目名": "导入三位测试", "检测指标": "ALT", "水平": "L",
             "批号": "LOT1", "日期": "2026-10-01", "测定值": 10.123456, "序号": 1},
            {"项目名": "导入三位测试", "检测指标": "ALT", "水平": "L",
             "批号": "LOT1", "日期": "2026-10-02", "测定值": 40, "序号": 1},
        ]
        status, res = _http(
            "POST", f"{base}/api/test_items/{ti['id']}/preview_import",
            raw=_make_xlsx(rows), headers={"X-Project-Name-Id": str(p["id"])},
        )
        assert status == 200
        vals = [float(r["value"]) for r in res["ok_rows"]]
        assert vals == [pytest.approx(10.123), pytest.approx(40.0)]
        assert _fmt3(vals[1]) == "40.000"


# ============================================================================
# D：前端展示统一三位小数
# ============================================================================

class TestFrontendThreeDecimals:
    """静态校验：所有测定值展示点统一调用 fmt3 输出三位小数。"""

    @pytest.fixture
    def html(self, web_server) -> str:
        base, _ = web_server
        with urlreq.urlopen(f"{base}/", timeout=5) as r:
            return r.read().decode("utf-8")

    def test_07_定义统一的fmt3格式化函数(self, html):
        assert "const fmt3" in html, "index.html 缺统一的 fmt3 三位小数格式化函数"

    def test_08_导入预览表格展示三位小数(self, html):
        assert "fmt3(r.value)" in html, "导入预览表未使用 fmt3 格式化测定值"

    def test_09_图表点位面板展示三位小数(self, html):
        assert "fmt3(d.value)" in html, "图表点位面板未使用 fmt3 格式化检测值"

    def test_10_明细列表与编辑弹窗展示三位小数(self, html):
        assert "fmt3(m.value)" in html, "测定值明细列表未使用 fmt3 格式化"
        idx = html.find("function showEditMeasurementModal")
        assert idx > 0, "未找到编辑测量弹窗函数"
        chunk = html[idx:idx + 2500]
        assert "fmt3(m.value)" in chunk, "编辑弹窗回填测定值未使用 fmt3"
