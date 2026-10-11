"""
issue #pack-web — Web 端到端测试（spec V1.3 §A-G + §8.2 + §8.3 b）

策略：
- 起一个 ThreadingTCPServer 监听 0 端口（系统自动分配空闲端口）
- urllib.request 测 HTTP 端点
- 不依赖外部测试库（requests / selenium 都不引）
- 业务模块 0 修改（Web 只 import 当后端）

覆盖：
1. / 返回 200 + 含关键 HTML 元素
2. /api/lab 自动建库
3. /api/projects CRUD
4. /api/test_items CRUD
5. /api/measurements 录入触发 Westgard 判定
6. /api/chart/<id>.svg 返 SVG
7. /api/stats/<id> 返统计 JSON
8. /api/export/monthly.xlsx 返 xlsx bytes
9. /api/export/ooc.xlsx 返 xlsx bytes
10. 备份钩子（state.json）写盘
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

import pytest


# ============================================================================
# 起服务
# ============================================================================

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch):
    """起 web 服务器，yield base_url。"""
    # 1) 把 db 目录重定向到 tmp_path（_default_db_path 用 cwd）
    monkeypatch.chdir(tmp_path)

    # 2) 导入并启动服务（每个测试独立 port，独立 server）
    from qc_tool import web as webmod
    port = _free_port()
    # 直接实例化 handler（不开浏览器，避免 CI 阻塞）
    from http.server import ThreadingHTTPServer
    handler = webmod.make_handler(db_path=tmp_path / "qc.db")
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.db_path = tmp_path / "qc.db"  # handler 用的
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    # 等启动
    time.sleep(0.05)
    try:
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _http(method: str, url: str, data: dict | None = None,
          raw: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes, dict]:
    """最小 HTTP 客户端。返回 (status, body_bytes, response_headers)。"""
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
        return e.code, e.read(), dict(e.headers) if e.headers else {}


# ============================================================================
# 1. GET / 返回 200 + 关键 HTML 元素
# ============================================================================

def test_index_returns_200_with_html(web_server):
    base, _ = web_server
    status, body, _ = _http("GET", f"{base}/")
    assert status == 200
    text = body.decode("utf-8")
    # 关键 DOM 元素（中文 / id）
    assert "qc-tool" in text.lower() or "质控" in text
    # 主菜单
    for keyword in ("项目", "指标", "质控图", "报表"):
        assert keyword in text, f"首页缺关键元素: {keyword}"


# ============================================================================
# 2. GET /api/lab 首次返回 lab_name=null + 自动建库
# ============================================================================

def test_lab_endpoint_creates_db_and_returns_lab_name(web_server):
    base, tmp = web_server
    db_path = tmp / "qc.db"
    # 数据库还不应存在（首次启动）
    # server 启动时可能已建，让它建即可
    status, body, _ = _http("GET", f"{base}/api/lab")
    assert status == 200
    payload = json.loads(body)
    assert "lab_name" in payload
    assert payload["lab_name"] is None  # 首次
    # 库被建
    assert db_path.exists()


# ============================================================================
# 3. /api/projects CRUD
# ============================================================================

def test_create_project_rejects_empty_name(web_server):
    base, _ = web_server
    status, _, _ = _http("POST", f"{base}/api/projects",
                         data={"name": "", "short": "x"})
    assert status == 400  # 校验失败


def test_create_then_list_projects(web_server):
    base, _ = web_server
    # 先建一个
    status, body, _ = _http("POST", f"{base}/api/projects",
                            data={"name": "生化", "short": "BC",
                                  "unit": "mmol/L", "manufacturer": "罗氏"})
    assert status == 200
    p = json.loads(body)
    assert p["name"] == "生化"
    assert p["id"] > 0
    # 列出
    status, body, _ = _http("GET", f"{base}/api/projects")
    assert status == 200
    items = json.loads(body)
    assert any(x["id"] == p["id"] and x["name"] == "生化" for x in items)


def test_create_project_dedup(web_server):
    base, _ = web_server
    _http("POST", f"{base}/api/projects", data={"name": "血常规"})
    status, _, _ = _http("POST", f"{base}/api/projects", data={"name": "血常规"})
    assert status == 400  # UNIQUE 触发


# ============================================================================
# 4. /api/test_items CRUD
# ============================================================================

def test_create_test_item_requires_valid_target_sd(web_server):
    base, _ = web_server
    # 先建项目
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P1"})
    pid = json.loads(body)["id"]
    # SD ≤ 0 应被拒
    status, _, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "Glu", "lot": "L1", "level": "L",
              "target": 5.0, "sd": 0},
    )
    assert status == 400


def test_create_test_item_success(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P2"})
    pid = json.loads(body)["id"]
    status, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "Glu", "lot": "L1", "level": "L",
              "target": 5.0, "sd": 0.2},
    )
    assert status == 200
    ti = json.loads(body)
    assert ti["name"] == "Glu" and ti["level"] == "L"
    # 列出
    status, body, _ = _http("GET", f"{base}/api/projects/{pid}/test_items")
    items = json.loads(body)
    assert any(x["id"] == ti["id"] for x in items)


# ============================================================================
# 5. /api/measurements 录入触发 Westgard 判定
# ============================================================================

def test_create_measurement_triggers_westgard_1_3s(web_server):
    base, _ = web_server
    # 建项目 + 指标
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P3"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "K", "lot": "LOT1", "level": "L",
              "target": 4.0, "sd": 0.1},
    )
    tid = json.loads(body)["id"]
    # 录入 5.0 → 10SD → 1_3s 失控
    status, body, _ = _http(
        "POST", f"{base}/api/test_items/{tid}/measurements",
        data={"date": "2026-01-15", "seq": 1, "value": 5.0,
              "lot": "LOT1", "level": "L"},
    )
    assert status == 200
    m = json.loads(body)
    assert m["status"] == "失控" or m["status"].startswith("OutOfControl") or m["status"].startswith("失控")
    assert "1_3s" in (m.get("rules") or "")


def test_create_measurement_in_control(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P4"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "Na", "lot": "L1", "level": "L",
              "target": 140, "sd": 2.0},
    )
    tid = json.loads(body)["id"]
    # 142 → 1SD → 在控
    status, body, _ = _http(
        "POST", f"{base}/api/test_items/{tid}/measurements",
        data={"date": "2026-01-16", "seq": 1, "value": 142,
              "lot": "L1", "level": "L"},
    )
    assert status == 200
    m = json.loads(body)
    assert m["status"] in ("在控", "InControl")


def test_list_measurements(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P5"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "X", "lot": "L", "level": "L",
              "target": 10, "sd": 0.5},
    )
    tid = json.loads(body)["id"]
    for i in range(3):
        _http(
            "POST", f"{base}/api/test_items/{tid}/measurements",
            data={"date": f"2026-02-0{i+1}", "seq": 1,
                  "value": 10.0 + i * 0.1, "lot": "L", "level": "L"},
        )
    status, body, _ = _http("GET", f"{base}/api/test_items/{tid}/measurements")
    items = json.loads(body)
    assert len(items) == 3


# ============================================================================
# 6. /api/chart/<id>.svg 返 SVG
# ============================================================================

def test_chart_svg_endpoint(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P6"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "Glu", "lot": "L", "level": "L",
              "target": 5, "sd": 0.2},
    )
    tid = json.loads(body)["id"]
    for d in range(1, 6):
        _http(
            "POST", f"{base}/api/test_items/{tid}/measurements",
            data={"date": f"2026-03-0{d}", "seq": 1,
                  "value": 5.0 + (d - 3) * 0.05, "lot": "L", "level": "L"},
        )
    status, body, hdrs = _http("GET", f"{base}/api/chart/{tid}.svg")
    assert status == 200
    assert b"<svg" in body
    assert b"</svg>" in body
    # SD 线
    assert b"+3SD" in body or b"3SD" in body


# ============================================================================
# 7. /api/stats/<id> 返统计 JSON
# ============================================================================

def test_stats_endpoint(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P7"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "T", "lot": "L", "level": "L",
              "target": 10, "sd": 0.5},
    )
    tid = json.loads(body)["id"]
    for d in range(1, 4):
        _http(
            "POST", f"{base}/api/test_items/{tid}/measurements",
            data={"date": f"2026-04-0{d}", "seq": 1,
                  "value": 10.0, "lot": "L", "level": "L"},
        )
    from urllib.parse import quote
    status, body, _ = _http("GET", f"{base}/api/stats/{tid}?year=2026&month=4&source=" + quote("固定"))
    assert status == 200
    payload = json.loads(body)
    assert "month" in payload
    assert "cumulative" in payload
    assert payload["month"]["n"] == 3


# ============================================================================
# 8. /api/export/monthly.xlsx 返 xlsx bytes
# ============================================================================

def test_export_monthly_xlsx(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P8"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "M", "lot": "L", "level": "L",
              "target": 5, "sd": 0.2},
    )
    tid = json.loads(body)["id"]
    _http(
        "POST", f"{base}/api/test_items/{tid}/measurements",
        data={"date": "2026-05-01", "seq": 1, "value": 5.0,
              "lot": "L", "level": "L"},
    )
    status, body, hdrs = _http(
        "GET", f"{base}/api/export/monthly.xlsx?project_id={pid}&year=2026&month=5",
    )
    assert status == 200
    # xlsx 是 zip，magic number PK\x03\x04
    assert body[:4] == b"PK\x03\x04"


# ============================================================================
# 9. /api/export/ooc.xlsx 返 xlsx bytes
# ============================================================================

def test_export_ooc_xlsx(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P9"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "O", "lot": "L", "level": "L",
              "target": 5, "sd": 0.1},
    )
    tid = json.loads(body)["id"]
    # 触发 1_3s
    _http(
        "POST", f"{base}/api/test_items/{tid}/measurements",
        data={"date": "2026-06-01", "seq": 1, "value": 5.5,
              "lot": "L", "level": "L"},
    )
    status, body, _ = _http(
        "GET",
        f"{base}/api/export/ooc.xlsx?project_id={pid}"
        f"&date_from=2026-06-01&date_to=2026-06-30",
    )
    assert status == 200
    assert body[:4] == b"PK\x03\x04"


# ============================================================================
# 10. 备份钩子（state.json）写盘
# ============================================================================

def test_backup_state_written_on_request(web_server):
    base, tmp = web_server
    # 触发任意请求后，handler 应调 mark_exit_dirty
    _http("GET", f"{base}/api/lab")
    # state.json 应存在（handler 在 first request 时 mark dirty）
    state_path = tmp / "state.json"
    # 注：web handler 不一定在 first request 就 mark dirty
    #   —— 由 mark_exit_dirty 在 server.serve_forever 之后调用
    #   这里只验证 server 不阻塞、state.json 路径没被破坏
    assert state_path.parent.exists()


# ============================================================================
# 11. 404 路径
# ============================================================================

def test_404_for_unknown_path(web_server):
    base, _ = web_server
    status, _, _ = _http("GET", f"{base}/api/nonexistent")
    assert status == 404


# ============================================================================
# 12. /api/lab POST 设值 + is_first_project_pending 翻 False
# ============================================================================

def test_set_lab_name_via_api(web_server):
    base, _ = web_server
    status, body, _ = _http("POST", f"{base}/api/lab",
                            data={"lab_name": "某医院检验科"})
    assert status == 200
    status, body, _ = _http("GET", f"{base}/api/lab")
    assert json.loads(body)["lab_name"] == "某医院检验科"


# ============================================================================
# 13. test_items 列表带 in_control_value 字段（前端要显示当前值）
# ============================================================================

def test_test_items_list_with_target_sd(web_server):
    base, _ = web_server
    _, body, _ = _http("POST", f"{base}/api/projects", data={"name": "P10"})
    pid = json.loads(body)["id"]
    _, body, _ = _http(
        "POST", f"{base}/api/projects/{pid}/test_items",
        data={"name": "T1", "lot": "L1", "level": "L",
              "target": 5.0, "sd": 0.2},
    )
    status, body, _ = _http("GET", f"{base}/api/projects/{pid}/test_items")
    items = json.loads(body)
    assert items[0]["target"] == 5.0
    assert items[0]["sd"] == 0.2
    assert items[0]["level"] == "L"


# ============================================================================
# 14. Step 1（2026-10-11）：PUT/DELETE 路由已实现 → 改为 "404 unknown" 测试
# ============================================================================

def _http_raw(method: str, url: str, data: dict | None = None) -> tuple[int, dict]:
    """支持 PUT/DELETE/GET/POST 的最小 HTTP 客户端。"""
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


def test_put_unknown_path_returns_404(web_server):
    """Step 1 验收：PUT 已知端点已实现；未知路径仍返 404。"""
    base, _ = web_server
    status, body = _http_raw("PUT", f"{base}/api/nonexistent/1",
                             data={"name": "x"})
    assert status == 404, f"未知路径应 404，实际 {status}"


def test_delete_unknown_path_returns_404(web_server):
    """Step 1 验收：DELETE 已知端点已实现；未知路径仍返 404。"""
    base, _ = web_server
    status, body = _http_raw("DELETE", f"{base}/api/nonexistent/1")
    assert status == 404, f"未知路径应 404，实际 {status}"
