"""
PR#1 — 用户需求 2026-10-14：点击质控图上的点 → 图表右侧展示排查面板
（在控：日期/序号/检测值/状态；警告：+触发规则；失控：+触发规则+spec §F.1 排查清单）

覆盖：
1. GET /api/investigation/plan?rules=... 按 spec §F.1 原文分类返回清单
   （1_3s→随机误差嫌疑 5 项；4_1s→系统误差嫌疑 6 项；2_2s→双向；多规则合并）
2. 单水平图 / L+H 双轴图 SVG 每个点带 data-* 点击属性 + 透明热区
3. HTML 静态扫描：右侧排查面板、点击事件委托、清单渲染逻辑
4. 旧排查弹窗 openInvestigation 清单内容统一走后端 plan 接口（spec 原文）
"""
from __future__ import annotations

import json
import re
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


def _post_json(base: str, path: str, payload: dict) -> dict:
    req = urlreq.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urlreq.urlopen(req, timeout=5).read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    return json.loads(urlreq.urlopen(f"{base}{path}", timeout=5).read().decode("utf-8"))


def _get_svg(base: str, tid: int, year: int, month: int) -> str:
    return urlreq.urlopen(
        f"{base}/api/chart/{tid}.svg?year={year}&month={month}", timeout=5
    ).read().decode("utf-8")


# spec §F.1 原文清单（与 qc_tool/investigation.py 一致）
_RANDOM_ITEMS = [
    "试剂：是否新开瓶 / 是否过期 / 是否污染",
    "仪器：泵阀、管道气泡、光路、电压",
    "操作：加样量、混匀、读数时机",
    "质控品：复溶是否充分 / 是否污染",
    "加样/混匀/读数等单次操作失误",
]
_SYSTEM_ITEMS = [
    "校准：是否近期校准 / 标准曲线是否漂移",
    "试剂：批号更换 / 厂家变更",
    "质控品：靶值与当前批是否一致",
    "环境：温度、湿度",
    "方法学偏倚：自身 2s 范围只代表随机误差水平，需结合方法学偏倚判断",
    "耗材：批号更换 / 厂家变更",
]


# ============================================================================
# GET /api/investigation/plan —— spec §F.1 分类清单
# ============================================================================

def test_plan_1_3s_random_error(web_server):
    """1_3s → 「随机误差嫌疑」i–v 全部 5 项（spec §F.1 验收 a）。"""
    base, _ = web_server
    plan = _get(base, "/api/investigation/plan?rules=1_3s")
    cats = {c["category"]: c["items"] for c in plan["categories"]}
    assert set(cats) == {"随机误差嫌疑"}
    assert cats["随机误差嫌疑"] == _RANDOM_ITEMS


def test_plan_4_1s_system_error(web_server):
    """4_1s → 「系统误差嫌疑」i–vi 全部 6 项。"""
    base, _ = web_server
    plan = _get(base, "/api/investigation/plan?rules=4_1s")
    cats = {c["category"]: c["items"] for c in plan["categories"]}
    assert set(cats) == {"系统误差嫌疑"}
    assert cats["系统误差嫌疑"] == _SYSTEM_ITEMS


def test_plan_10_x_system_error(web_server):
    """10_x → 系统误差嫌疑。"""
    base, _ = web_server
    plan = _get(base, "/api/investigation/plan?rules=10_x")
    assert [c["category"] for c in plan["categories"]] == ["系统误差嫌疑"]


def test_plan_2_2s_both_directions(web_server):
    """2_2s → 随机误差 + 系统误差两个方向同时排查。"""
    base, _ = web_server
    plan = _get(base, "/api/investigation/plan?rules=2_2s")
    assert [c["category"] for c in plan["categories"]] == ["随机误差嫌疑", "系统误差嫌疑"]


def test_plan_multi_rules_merged(web_server):
    """spec §F.1 e：同时触发 1_3s + 2_2s → 合并展示随机+系统（不重复）。"""
    base, _ = web_server
    plan = _get(base, "/api/investigation/plan?rules=1_3s,2_2s")
    assert [c["category"] for c in plan["categories"]] == ["随机误差嫌疑", "系统误差嫌疑"]


def test_plan_accepts_json_array_rules(web_server):
    """旧排查弹窗传入的 m.rules 是 JSON 数组字符串，接口需兼容。"""
    base, _ = web_server
    plan = _get(base, "/api/investigation/plan?rules=%5B%221_3s%22%2C%224_1s%22%5D")
    assert [c["category"] for c in plan["categories"]] == ["随机误差嫌疑", "系统误差嫌疑"]


def test_plan_unknown_rule_returns_empty(web_server):
    """未知规则代码 → 空分类清单，200 而非 500。"""
    base, _ = web_server
    plan = _get(base, "/api/investigation/plan?rules=NOT_A_RULE")
    assert plan["categories"] == []


# ============================================================================
# SVG 点可点击（单水平图 + L+H 双轴图）
# ============================================================================

def _setup_single(base: str, project: str, name: str, lot: str,
                  target=10.0, sd=1.0) -> int:
    pid = _post_json(base, "/api/projects", {"name": project})["id"]
    return _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": name, "level": "L", "lot": lot, "target": target, "sd": sd,
    })["id"]


def _add_measurement(base: str, tid: int, *, date: str, value: float,
                     lot: str, level: str, seq: int = 1) -> dict:
    return _post_json(base, f"/api/test_items/{tid}/measurements", {
        "date": date, "seq": seq, "value": value, "lot": lot, "level": level})


def test_svg_single_points_clickable(web_server):
    """单水平图：每个点外包 <g class="pt" data-*> + 透明热区，供点击面板读取。"""
    base, _ = web_server
    tid = _setup_single(base, project="点击面板项目", name="ALT", lot="LOT2026A")
    # 在控 10.2 / 警告 12.2(+2.2SD) / 在控 9.8 / 失控 13.5(+3.5SD)
    m1 = _add_measurement(base, tid, date="2026-05-01", value=10.2,
                          lot="LOT2026A", level="L")
    m2 = _add_measurement(base, tid, date="2026-05-02", value=12.2,
                          lot="LOT2026A", level="L")
    _add_measurement(base, tid, date="2026-05-03", value=9.8,
                     lot="LOT2026A", level="L")
    m4 = _add_measurement(base, tid, date="2026-05-04", value=13.5,
                          lot="LOT2026A", level="L")

    svg = _get_svg(base, tid, 2026, 5)

    # 4 个点各一个 <g class="pt">
    assert svg.count('class="pt"') == 4
    # 每个点带透明热区（放大点击命中半径）
    assert svg.count('r="11" fill="transparent"') == 4
    # 在控点：data-mid + 状态 in_control，无规则
    assert f'data-mid="{m1["id"]}"' in svg
    assert f'data-status="in_control" data-rules="" data-level="L"' in svg
    # 警告点：状态 warning + 触发规则 1_2s
    assert f'data-mid="{m2["id"]}"' in svg
    assert f'data-status="warning" data-rules="1_2s"' in svg
    # 失控点：状态 out_of_control + 触发规则含 1_3s（判定层同时记录 1_2s 警告标记）
    assert f'data-mid="{m4["id"]}"' in svg
    assert re.search(rf'data-mid="{m4["id"]}"[^>]*data-status="out_of_control"', svg)
    assert re.search(rf'data-mid="{m4["id"]}"[^>]*data-rules="[^"]*1_3s[^"]*"', svg)
    # 检测日期 / 序号 / 检测值 属性齐全
    assert 'data-date="2026-05-04"' in svg
    assert 'data-seq="1"' in svg
    assert 'data-value="13.500"' in svg


def test_svg_dual_points_clickable(web_server):
    """L+H 双轴图：L/H 点都带 data-level，各自可点击。"""
    base, _ = web_server
    pid = _post_json(base, "/api/projects", {"name": "双轴点击项目"})["id"]
    tid_l = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": "ALT", "level": "L", "lot": "LOT2026A", "target": 10.0, "sd": 1.0,
    })["id"]
    tid_h = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": "ALT", "level": "H", "lot": "LOT2026A", "target": 50.0, "sd": 5.0,
    })["id"]
    ml = _add_measurement(base, tid_l, date="2026-05-01", value=10.5,
                          lot="LOT2026A", level="L")
    mh = _add_measurement(base, tid_h, date="2026-05-01", value=51.0,
                          lot="LOT2026A", level="H")

    svg = _get_svg(base, tid_l, 2026, 5)

    assert svg.count('class="pt"') == 2
    assert f'data-mid="{ml["id"]}"' in svg
    assert f'data-mid="{mh["id"]}"' in svg
    assert f'data-level="L"' in svg
    assert f'data-level="H"' in svg


# ============================================================================
# HTML 静态扫描：右侧排查面板 + 点击委托 + 清单渲染
# ============================================================================

def _load_html() -> str:
    from qc_tool.web import _load_template
    return _load_template().decode("utf-8")


def test_html_has_point_panel_layout():
    """右侧排查面板存在：id=point-panel + 空态提示 + 图表内联容器。"""
    html = _load_html()
    assert 'id="point-panel"' in html, "缺右侧排查面板容器"
    assert 'id="point-panel-body"' in html, "缺面板内容容器"
    assert "点击图上数据点查看排查信息" in html, "缺面板空态提示"
    assert 'id="chart-svg-box"' in html, "图表应改为内联 SVG 容器（点击需要）"
    assert 'id="chart-img"' not in html, "旧 <img> 方案应移除（img 无法捕获点点击）"
    # 内联 SVG 必须按容器缩放（2026-10-14 修复：固定宽度 SVG 在窄窗口被
    # flex 居中溢出裁到负坐标 → 左侧点不可点击）
    assert '#chart-svg-box svg' in html, "缺内联 SVG 缩放规则选择器"
    assert 'max-width: 100%' in html, "SVG 应 max-width:100% 随容器缩放"
    # 窄窗口下面板换行到图下方，图表保底 420px（避免被压到无法点击）
    assert 'flex-wrap:wrap' in html, "图表行应允许换行"
    assert 'min-width:420px' in html, "图表容器应有最小宽度"


def test_html_chart_click_delegation():
    """refreshChart 内联注入 SVG + 事件委托绑定 data-mid 点击 → showPointPanel。"""
    html = _load_html()
    assert "bindChartClicks" in html
    assert 'closest("[data-mid]")' in html, "点击应委托到 [data-mid] 元素"
    assert "showPointPanel" in html
    assert "$(\"chart-svg-box\").innerHTML" in html or \
        '$("chart-svg-box").innerHTML' in html, "SVG 应内联注入"
    assert "bindChartClicks();" in html, "init 应绑定图点击事件"


def test_html_point_panel_renders_status_and_rules():
    """面板按状态渲染：检测日期/序号/检测值/状态 + 触发规则 + 失控排查清单。"""
    html = _load_html()
    assert "检测日期" in html and "序号" in html and "检测值" in html
    assert "POINT_STATUS_LABEL" in html, "状态应为中文在控/警告/失控"
    assert "触发规则" in html
    assert "排查清单（spec §F.1）" in html
    assert "loadOocPanel" in html and "renderOocView" in html, \
        "失控点应走查看/编辑两模式渲染"
    assert "/api/investigation/plan" in html, "清单应走后端 plan 接口"


def test_html_export_png_uses_inline_svg():
    """导出 PNG 改为序列化内联 SVG（XMLSerializer），不再依赖 img.src。"""
    html = _load_html()
    assert "XMLSerializer" in html
    assert 'querySelector("#chart-svg-box svg")' in html


def test_html_open_investigation_uses_spec_plan():
    """旧排查弹窗清单内容统一为 spec §F.1 原文（走后端 plan 接口，保留勾选+保存）。"""
    html = _load_html()
    # 旧硬编码清单已移除
    assert "重测控制品" not in html, "旧硬编码清单应移除"
    assert "检查吸样/加样误差" not in html
    # openInvestigation 改走 plan 接口
    assert "openInvestigation" in html
    assert 'plan.categories' in html, "弹窗应渲染 plan 接口返回的分类"
    # 勾选 + 保存保留（spec §F.1 b/c/d，2026-10-14 用户拍板）
    assert "inv-cb" in html
    assert "saveInvestigation" in html
    assert "inv-conclusion" in html


# ============================================================================
# 排查面板联动 F.1：编辑保存 → 点击只展示已勾选（2026-10-14 用户补充裁定）
# ============================================================================

def test_GET_ooc_investigation_empty_before_save(web_server):
    """未保存过排查项 → GET 返回空 checklist / conclusion=None。"""
    base, _ = web_server
    tid = _setup_single(base, project="联动项目", name="ALT", lot="LOT1")
    m = _add_measurement(base, tid, date="2026-05-04", value=13.5,
                         lot="LOT1", level="L")
    rec = _get(base, f"/api/ooc/{m['id']}/investigation")
    assert rec["measurement_id"] == m["id"]
    assert rec["checklist"] == {}
    assert rec["conclusion"] is None


def test_save_then_get_roundtrip(web_server):
    """面板保存（POST /investigate）→ GET 回读：只含用户勾选的项 + 结论。"""
    base, _ = web_server
    tid = _setup_single(base, project="联动项目2", name="ALT", lot="LOT1")
    m = _add_measurement(base, tid, date="2026-05-04", value=13.5,
                         lot="LOT1", level="L")
    _post_json(base, f"/api/ooc/{m['id']}/investigate", {
        "items": [
            {"category": "随机误差嫌疑", "item": "试剂：是否新开瓶 / 是否过期 / 是否污染"},
            {"category": "系统误差嫌疑", "item": "校准：是否近期校准 / 标准曲线是否漂移"},
        ],
        "conclusion": "换新开瓶试剂后复测",
    })
    rec = _get(base, f"/api/ooc/{m['id']}/investigation")
    assert rec["checklist"]["随机误差嫌疑"] == ["试剂：是否新开瓶 / 是否过期 / 是否污染"]
    assert rec["checklist"]["系统误差嫌疑"] == ["校准：是否近期校准 / 标准曲线是否漂移"]
    assert rec["conclusion"] == "换新开瓶试剂后复测"
    # 未勾选的项不出现在记录里
    assert "仪器：泵阀、管道气泡、光路、电压" not in json.dumps(rec["checklist"],
                                                          ensure_ascii=False)


def test_html_panel_edit_save_two_modes():
    """面板两模式静态扫描：编辑按钮、勾选、保存、回显已勾选、联动接口。"""
    html = _load_html()
    assert "编辑排查清单" in html, "失控点面板应有编辑入口"
    assert "尚未记录排查项" in html, "未保存时查看模式应提示"
    assert "pt-inv-cb" in html, "编辑模式应有可勾选清单"
    assert "saveOocPanel" in html, "应有保存排查记录逻辑"
    assert "cancelOocEdit" in html, "应有取消编辑逻辑"
    assert "normalizeChecklist" in html, "应兼容两种落库格式（web/桌面弹窗）"
    assert 'api("GET", `/api/ooc/' in html, "应联动读取已保存排查记录"
    assert 'api("POST", `/api/ooc/' in html, "应复用 F.1 保存接口"
    assert "请至少勾选一项检查项再保存" in html, "保留 spec §F.1 d 必勾校验"


# ============================================================================
# 端到端：点数据 → 面板数据源闭环
# ============================================================================

def test_e2e_point_click_data_flow(web_server):
    """SVG 点的 data-* 属性足以渲染右侧面板：日期/序号/值/状态/规则/水平全齐。"""
    base, _ = web_server
    tid = _setup_single(base, project="闭环项目", name="ALP", lot="LOTX")
    m = _add_measurement(base, tid, date="2026-06-01", value=13.5,
                         lot="LOTX", level="L", seq=3)

    svg = _get_svg(base, tid, 2026, 6)
    g = re.search(rf'<g class="pt"[^>]*data-mid="{m["id"]}"[^>]*>', svg)
    assert g, "失控点应外包 <g class=pt data-mid=...>"
    attrs = g.group(0)
    for key, val in [("data-date", "2026-06-01"), ("data-seq", "3"),
                     ("data-value", "13.500"), ("data-status", "out_of_control"),
                     ("data-level", "L")]:
        assert f'{key}="{val}"' in attrs, f"缺 {key}={val}"
    assert 'data-rules="' in attrs and "1_3s" in attrs, "失控点应带触发规则 1_3s"

    # 面板清单来自 plan 接口：1_3s → 随机误差嫌疑 5 项
    plan = _get(base, "/api/investigation/plan?rules=1_3s")
    assert plan["categories"][0]["category"] == "随机误差嫌疑"
    assert len(plan["categories"][0]["items"]) == 5
