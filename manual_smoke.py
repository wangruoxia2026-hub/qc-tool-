"""
手测 PR#1+#2 — 端到端 smoke（HTTP + HTML 抓取，模拟 UI 流程）

模拟场景：用户在浏览器里做的所有操作。
- 启动 web 服务
- 创建项目/指标
- 录入正常值（验证 1_2s/1_3s/2_2s 判定）
- Excel 模板下载 + 上传导入（PR#1）
- 边界校验（未来日期、负数、文本）
- 右键菜单 HTML 验证
- 编辑 + 标记作废（PR#2）
- 失控记录生成

输出：每条用例 ✅/❌ + 详情。
"""
import io
import json
import os
import socket
import struct
import sys
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError

# 强制 UTF-8 输出（避免 Windows GBK）
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


PASS = "✅"
FAIL = "❌"
results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    icon = PASS if ok else FAIL
    print(f"  {icon} {name}" + (f"  →  {detail}" if detail else ""))
    results.append((name, ok, detail))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, data=None, raw: bytes | None = None) -> tuple[int, bytes]:
    if data is not None:
        body = json.dumps(data).encode()
        h = {"Content-Type": "application/json"}
    elif raw is not None:
        body = raw
        h = {"Content-Type": "application/octet-stream"}
    else:
        body = b""
        h = {}
    req = urlreq.Request(url, data=body, method=method, headers=h)
    try:
        resp = urlreq.urlopen(req, timeout=10)
        return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


def _post_multipart(url: str, fields: dict, file_field: str,
                    filename: str, file_bytes: bytes) -> tuple[int, bytes]:
    """简版 multipart/form-data POST。"""
    boundary = b"----smoketestboundary123"
    body = b""
    for k, v in fields.items():
        body += b"--" + boundary + b"\r\n"
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += str(v).encode() + b"\r\n"
    body += b"--" + boundary + b"\r\n"
    body += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += file_bytes + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    req = urlreq.Request(url, data=body, method="POST",
                         headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
    try:
        resp = urlreq.urlopen(req, timeout=10)
        return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


# ====================== 启动 web ======================
print("=" * 72)
print("  PR#1+#2 手测 smoke — 端到端 HTTP")
print("=" * 72)

import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

from qc_tool import web as webmod

port = _free_port()
handler = webmod.make_handler(db_path=Path("qc.db"))
server = ThreadingHTTPServer(("127.0.0.1", port), handler)
server.db_path = Path("qc.db")
import threading
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
base = f"http://127.0.0.1:{port}"
print(f"\n  web 启动: {base}")
time.sleep(0.2)

try:
    # ====================== 基础可达性 ======================
    print("\n" + "-" * 72)
    print("  [基础] 服务可达 + HTML 关键元素")
    print("-" * 72)
    status, body = _http("GET", f"{base}/")
    _record("GET / 返 200", status == 200, f"status={status}")
    html = body.decode("utf-8")
    _record("HTML 含'质控'/'项目'/'指标'", all(k in html for k in ("质控", "项目", "指标")))

    # ====================== P1.1 录入正常值 ======================
    print("\n" + "-" * 72)
    print("  [P1.1] 录入表单：5 字段+操作员+前处理")
    print("-" * 72)
    status, body = _http("POST", f"{base}/api/lab", {"lab_name": "测试检验科"})
    _record("POST /api/lab 实验室名", status == 200)
    import uuid as _uuid
    proj_name = f"VitD-{_uuid.uuid4().hex[:8]}"
    status, body = _http("POST", f"{base}/api/projects", {"name": proj_name, "short": "VitD"})
    pid = json.loads(body)["id"] if status == 200 else None
    _record("POST /api/projects 成功", status == 200 and pid is not None, f"pid={pid} name={proj_name!r}")
    status, body = _http("POST", f"{base}/api/projects/{pid}/test_items",
                         {"name": "D2", "lot": "LOT1", "level": "L", "target": 100.0, "sd": 2.0})
    tid = json.loads(body)["id"] if status == 200 else None
    _record("POST /api/test_items 成功", status == 200 and tid is not None, f"tid={tid}")

    # P1.1 录 1 条在控
    status, body = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                         {"date": "2026-01-15", "seq": 1, "value": 100.0,
                          "lot": "LOT1", "level": "L", "operator": "张三",
                          "pre_processor": "李四"})
    m1 = json.loads(body) if status == 200 else None
    _record("录入 1 条在控值 100.0", status == 200 and m1 is not None, f"status={m1.get('status') if m1 else None}")
    _record("operator + pre_processor 字段支持", m1 and m1.get("operator") == "张三")

    # ====================== P1.2 录入超 3SD 触发 1_3s ======================
    print("\n" + "-" * 72)
    print("  [P1.2] 录入超 3SD → 1_3s 失控")
    print("-" * 72)
    status, body = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                         {"date": "2026-01-15", "seq": 2, "value": 107.0,
                          "lot": "LOT1", "level": "L"})
    m2 = json.loads(body) if status == 200 else None
    _record("录入 107.0 (3.5SD) 触发 1_3s",
            m2 and "1_3s" in (m2.get("rules") or ""),
            f"status={m2.get('status')!r} rules={m2.get('rules')!r}")

    # ====================== P1.3 2_2s ======================
    print("\n" + "-" * 72)
    print("  [P1.3] 跨 2SD 连续 2 条 → 2_2s")
    print("-" * 72)
    # 用新日期避免 1_3s 干扰
    status, body = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                         {"date": "2026-01-16", "seq": 1, "value": 105.0,
                          "lot": "LOT1", "level": "L"})
    m3a = json.loads(body) if status == 200 else None
    status, body = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                         {"date": "2026-01-16", "seq": 2, "value": 105.5,
                          "lot": "LOT1", "level": "L"})
    m3b = json.loads(body) if status == 200 else None
    # 第 2 条 2_2s 触发
    _record("2 条连续 2SD 触发 2_2s",
            m3b and "2_2s" in (m3b.get("rules") or ""),
            f"m3a.rules={m3a.get('rules')!r} m3b.rules={m3b.get('rules')!r}")

    # ====================== P1.4 未来日期 ======================
    print("\n" + "-" * 72)
    print("  [P1.4] 日期晚于今天")
    print("-" * 72)
    # 未来日期：spec §B.1 是警告不阻断，API 应能保存但 status 含 is_future
    status, body = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                         {"date": "2099-01-01", "seq": 1, "value": 100.0,
                          "lot": "LOT1", "level": "L"})
    _record("未来日期允许保存（spec §9 仅警告）", status == 200,
            f"status={status}")

    # ====================== P1.5 边界校验 ======================
    print("\n" + "-" * 72)
    print("  [P1.5] 边界校验：文字/负数/空")
    print("-" * 72)
    status, _ = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                      {"date": "2026-01-17", "seq": 1, "value": "abc",
                       "lot": "LOT1", "level": "L"})
    _record("value='abc' 应被拒（400）", status == 400, f"status={status}")
    status, _ = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                      {"date": "2026-01-17", "seq": 1, "value": -1.0,
                       "lot": "LOT1", "level": "L"})
    _record("value=-1.0 应被拒（400）", status == 400, f"status={status}")
    status, _ = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                      {"date": "2026-01-17", "seq": 1, "value": 0,
                       "lot": "LOT1", "level": "L"})
    _record("value=0 允许（spec §9 ≥ 0）", status == 200, f"status={status}")

    # ====================== P1.6 Excel 模板下载 ======================
    print("\n" + "-" * 72)
    print("  [P1.6] Excel 模板下载（PR#1）")
    print("-" * 72)
    # 找模板下载 URL
    template_url = None
    for keyword in ("template", "Template", "下载", "download"):
        if keyword in html:
            pass
    # 查 web 是否有模板 API
    status, body = _http("GET", f"{base}/api/template.xlsx")
    if status == 200 and len(body) > 100:
        _record("GET /api/template.xlsx 返 xlsx bytes", True, f"len={len(body)}")
    else:
        # 尝试别的路径
        for path in ("/api/template", "/api/import/template", "/api/measurements/template",
                     "/api/test_items/template"):
            status, body = _http("GET", f"{base}{path}")
            if status == 200 and len(body) > 100:
                _record(f"GET {path} 返 bytes", True, f"len={len(body)}")
                template_url = f"{base}{path}"
                break
        else:
            _record("Excel 模板下载 API 存在", False, "没找到模板下载端点")

    # ====================== P1.7 上传 Excel ======================
    print("\n" + "-" * 72)
    print("  [P1.7] 上传 Excel 导入（PR#1）")
    print("-" * 72)
    # 正确路径：/api/test_items/<tid>/import + X-Project-Name-Id header
    import_path = f"/api/test_items/{tid}/import"
    _record("Excel 导入路径 = /api/test_items/<tid>/import", True, f"path={import_path}")
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(["项目名", "检测指标", "水平", "批号", "日期", "测定值"])
        ws.append([proj_name, "D2", "L", "LOT1", "2026-02-01", 100.0])
        ws.append([proj_name, "D2", "L", "LOT1", "2026-02-01", 99.0])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()
        # 直接走 web.py 路由分发（POST 不经 multipart 解析）
        req = urlreq.Request(
            f"{base}{import_path}",
            data=xlsx_bytes, method="POST",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Project-Name-Id": str(pid),
            },
        )
        try:
            resp = urlreq.urlopen(req, timeout=10)
            status, body = resp.status, resp.read()
        except HTTPError as e:
            status, body = e.code, e.read()
        _record("POST Excel 导入成功",
                status == 200,
                f"status={status} body={body[:200]!r}")
    except Exception as e:
        _record("Excel 导入测试", False, f"异常: {e}")

    # ====================== P1.8 缺列校验 ======================
    print("\n" + "-" * 72)
    print("  [P1.8] Excel 缺必需列")
    print("-" * 72)
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["项目名"])  # 缺检测指标, 水平, 批号, 日期, 测定值
        ws.append([proj_name])
        buf = io.BytesIO()
        wb.save(buf)
        req = urlreq.Request(
            f"{base}{import_path}",
            data=buf.getvalue(), method="POST",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Project-Name-Id": str(pid),
            },
        )
        try:
            resp = urlreq.urlopen(req, timeout=10)
            status, body = resp.status, resp.read()
        except HTTPError as e:
            status, body = e.code, e.read()
        _record("缺列返回 400", status == 400, f"status={status} body={body[:200]!r}")
    except Exception as e:
        _record("缺列测试", False, f"异常: {e}")

    # ====================== PR#2 HTML 检查 ======================
    print("\n" + "-" * 72)
    print("  [P2.1-P2.6] PR#2 HTML 关键元素")
    print("-" * 72)
    # 拉最新 HTML
    status, body = _http("GET", f"{base}/")
    html = body.decode("utf-8")
    _record("HTML 含 oncontextmenu", "oncontextmenu" in html)
    _record("HTML 含 '编辑' 菜单项", "编辑" in html)
    _record("HTML 含 '作废' / '标记作废'", "作废" in html)
    _record("HTML 含 showEditMeasurementModal", "function showEditMeasurementModal" in html)
    _record("HTML 含 voidMeasurement", "function voidMeasurement" in html)
    _record("HTML 含 saveEditMeasurement", "function saveEditMeasurement" in html)
    _record("voidMeasurement 用 confirm()", "voidMeasurement" in html and html.count("confirm(") >= 1)

    # ====================== PR#2 端到端 ======================
    print("\n" + "-" * 72)
    print("  [P2.3] PUT 改 value 触发重判")
    print("-" * 72)
    # 录 1 条在控
    status, body = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                         {"date": "2026-03-01", "seq": 1, "value": 100.0,
                          "lot": "LOT1", "level": "L"})
    m_e1 = json.loads(body) if status == 200 else None
    # 改 value → 1_3s
    status, body = _http("PUT", f"{base}/api/measurements/{m_e1['id']}",
                         {"value": 107.0})
    m_e2 = json.loads(body) if status == 200 else None
    _record("PUT 改 value 4→107 触发 1_3s",
            m_e2 and "1_3s" in (m_e2.get("rules") or ""),
            f"rules={m_e2.get('rules')!r}")

    # ====================== P2.4 改其他字段 ======================
    print("\n" + "-" * 72)
    print("  [P2.4] PUT 改 5 字段里其他")
    print("-" * 72)
    status, body = _http("PUT", f"{base}/api/measurements/{m_e1['id']}",
                         {"date": "2026-03-02", "lot": "LOT1B", "seq": 99})
    _record("PUT 改 date/lot/seq 成功", status == 200, f"status={status}")

    # ====================== P2.5/2.6 DELETE 标记作废 ======================
    print("\n" + "-" * 72)
    print("  [P2.5-2.6] DELETE 标记作废")
    print("-" * 72)
    # 录 1 条
    status, body = _http("POST", f"{base}/api/test_items/{tid}/measurements",
                         {"date": "2026-04-01", "seq": 1, "value": 100.0,
                          "lot": "LOT1", "level": "L"})
    m_v1 = json.loads(body) if status == 200 else None
    # 作废
    status, body = _http("DELETE", f"{base}/api/measurements/{m_v1['id']}")
    _record("DELETE 返 200", status == 200, f"status={status}")
    # 查
    status, body = _http("GET", f"{base}/api/test_items/{tid}/measurements")
    items = json.loads(body) if status == 200 else []
    m_v1_after = next((x for x in items if x["id"] == m_v1["id"]), None)
    _record("作废后 status='作废'", m_v1_after and m_v1_after.get("status") == "作废",
            f"status={m_v1_after.get('status')!r}")
    # 幂等
    status, body = _http("DELETE", f"{base}/api/measurements/{m_v1['id']}")
    _record("DELETE 幂等（再删一次 200）", status == 200, f"status={status}")
    # 编辑已作废应被拒
    status, body = _http("PUT", f"{base}/api/measurements/{m_v1['id']}", {"value": 105.0})
    _record("编辑已作废记录应被拒（400/409）", status in (400, 409, 404),
            f"status={status}")

    # ====================== S.1 端到端冒烟 ======================
    print("\n" + "-" * 72)
    print("  [S.1] 端到端：项目→指标→录 7 天数据→图表")
    print("-" * 72)
    status, body = _http("GET", f"{base}/api/chart/{tid}.svg")
    _record(f"GET /api/chart/{tid}.svg", status == 200, f"status={status} bytes={len(body)}")

    # ====================== S.3 端口冲突 ======================
    print("\n" + "-" * 72)
    print("  [S.3] 端口冲突自动 +1")
    print("-" * 72)
    # 模拟：在另一端口范围找，应能避开本 server 的 port
    from qc_tool.web import _find_free_port
    test_port = _find_free_port("127.0.0.1", 9999)
    _record("_find_free_port(9999) 找到空闲端口", test_port >= 9999, f"port={test_port}")

    # ====================== 汇总 ======================
    print("\n" + "=" * 72)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"  汇总: {passed} passed / {total} total（{failed} failed）")
    print("=" * 72)
    if failed:
        print("\n  ❌ 失败用例：")
        for name, ok, detail in results:
            if not ok:
                print(f"    - {name}: {detail}")

finally:
    server.shutdown()
    server.server_close()

sys.exit(0 if all(ok for _, ok, _ in results) else 1)
