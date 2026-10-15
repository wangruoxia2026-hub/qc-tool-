"""
自动化测试：Web 端质控图【一图双 y 轴】展示
（用户需求 2026-10-12 第二次补充：左轴 L、右轴 H，控制限像素级重合，
取代并排双图；L 点绿/黄/红圆，H 点蓝/黄/红三角）。

Run:
    python -m pytest tests/test_web_pair_chart.py -v
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


def _post_json(base: str, path: str, payload: dict) -> dict:
    req = urlreq.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urlreq.urlopen(req, timeout=5).read().decode("utf-8"))


def _get_svg(base: str, tid: int, year: int, month: int) -> str:
    return urlreq.urlopen(
        f"{base}/api/chart/{tid}.svg?year={year}&month={month}", timeout=5
    ).read().decode("utf-8")


def _setup_pair(base: str, *, project: str, name: str, lot: str,
                target_l=10.0, sd_l=1.0, target_h=50.0, sd_h=5.0):
    """创建项目 + 同名同批号 L/H 两水平；返回 (tid_l, tid_h)。"""
    pid = _post_json(base, "/api/projects", {"name": project})["id"]
    tid_l = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": name, "level": "L", "lot": lot,
        "target": target_l, "sd": sd_l,
    })["id"]
    tid_h = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": name, "level": "H", "lot": lot,
        "target": target_h, "sd": sd_h,
    })["id"]
    return tid_l, tid_h


def _add_measurement(base: str, tid: int, *, date: str, value: float,
                     lot: str, level: str):
    _post_json(base, f"/api/test_items/{tid}/measurements", {
        "date": date, "seq": 1, "value": value, "lot": lot, "level": level})


def _control_line_y_counts(svg: str, color: str, dash: str) -> dict:
    """统计某色某线型的控制限横线（x1=78 为绘图区左缘）的 y1 出现次数。"""
    pat = rf'x1="78" y1="([\d.]+)"[^>]*stroke="{color}"[^>]*stroke-dasharray="{dash}"'
    ys = re.findall(pat, svg)
    counts: dict[str, int] = {}
    for y in ys:
        counts[y] = counts.get(y, 0) + 1
    return counts


# ============================================================================
# 一图双 y 轴 SVG
# ============================================================================

def test_dual_axis_svg_rendering(web_server):
    """L+H 同时存在时，SVG 呈一图双 y 轴：左轴 L、右轴 H，控制限重合。"""
    base, _ = web_server
    tid_l, tid_h = _setup_pair(base, project="生化全套", name="ALT", lot="LOT2026A")

    for d, v in [("2026-05-01", 10.5), ("2026-05-02", 9.8)]:
        _add_measurement(base, tid_l, date=d, value=v, lot="LOT2026A", level="L")
    for d, v in [("2026-05-01", 51.0), ("2026-05-02", 48.0)]:
        _add_measurement(base, tid_h, date=d, value=v, lot="LOT2026A", level="H")

    svg = _get_svg(base, tid_l, 2026, 5)

    # 1. 水平说明：左轴 L、右轴 H
    assert "低水平 L（左轴）" in svg
    assert "高水平 H（右轴）" in svg

    # 2. 单一绘图区（双轴合图，不再是并排两白底）
    assert svg.count('fill="white"') == 1, "应为一张图一个白底绘图区"

    # 3. 左右两套浓度刻度：L 靶值 10.00 / +3SD 13.00 / -3SD 7.00；
    #    H 靶值 50.00 / +3SD 65.00 / -3SD 35.00
    for lbl in ["10.00（靶值）", "13.00（+3SD）", "7.00（-3SD）",
                "50.00（靶值）", "65.00（+3SD）", "35.00（-3SD）"]:
        assert lbl in svg, f"刻度 {lbl} 应存在"

    # 4. 控制限重合：同一 k·SD 线左右各画一次且 y1 像素相同
    for color, dash, expect in [
        ("#c0392b", "", {"+3SD": 2, "-3SD": 2}),   # 红实线：±3SD
        ("#d9a400", "4,2", {"+2SD": 2, "-2SD": 2}),  # 黄虚线：±2SD
        ("#1f7a1f", "2,2", {"+1SD": 2, "-1SD": 2}),  # 绿虚线：±1SD
        ("#1f7a1f", "", {"靶值": 2}),               # 绿实线：中心
    ]:
        counts = _control_line_y_counts(svg, color, dash)
        assert sum(counts.values()) == sum(expect.values()), \
            f"{color}/{dash}: 线数应等于 {sum(expect.values())}，got {counts}"
        assert set(counts.values()) == {2}, \
            f"{color}/{dash}: 每条 k·SD 线应左右各画一次且 y 重合，got {counts}"

    # 5. 点位提示带水平前缀
    assert "[L水平]" in svg
    assert "[H水平]" in svg

    # 6. 用 tid_h 请求同样得到双轴图
    svg_h = _get_svg(base, tid_h, 2026, 5)
    assert "低水平 L（左轴）" in svg_h
    assert "高水平 H（右轴）" in svg_h


def test_same_date_lh_aligned_x(web_server):
    """横轴对齐：同一日期的 L、H 点严格同一 x（2026-10-13 用户第三次调整）。"""
    base, _ = web_server
    tid_l, tid_h = _setup_pair(base, project="对齐测试项目", name="ALT", lot="LOT2026A")
    for d, v in [("2026-05-01", 10.5), ("2026-05-03", 9.8)]:
        _add_measurement(base, tid_l, date=d, value=v, lot="LOT2026A", level="L")
    for d, v in [("2026-05-01", 51.0), ("2026-05-02", 48.0)]:
        _add_measurement(base, tid_h, date=d, value=v, lot="LOT2026A", level="H")

    svg = _get_svg(base, tid_l, 2026, 5)

    # 点位元素（带 <title> 悬停提示），取各自的中心 x
    lx_by = {d: float(x) for x, d in re.findall(
        r'<circle cx="([\d.]+)" cy="[\d.]+" r="4\.5"[^>]*><title>\[L水平\] ([\d-]+) seq=',
        svg)}
    hx_by = {d: float(x) for x, d in re.findall(
        r'<polygon points="([\d.]+),[\d.]+[^"]*"[^>]*><title>\[H水平\] ([\d-]+) seq=',
        svg)}
    assert set(lx_by) == {"2026-05-01", "2026-05-03"}
    assert set(hx_by) == {"2026-05-01", "2026-05-02"}
    # 同日 L/H 同一 x；横位按日期递增
    assert lx_by["2026-05-01"] == hx_by["2026-05-01"]
    assert lx_by["2026-05-01"] < hx_by["2026-05-02"] < lx_by["2026-05-03"]


def test_same_date_multi_points_pairwise_aligned_x(web_server):
    """同日多条：第 i 个 L 点与第 i 个 H 点共用同一 x（2026-10-13 第四次调整）。"""
    base, _ = web_server
    tid_l, tid_h = _setup_pair(base, project="配对测试项目", name="ALT", lot="LOT2026A")
    for v in (10.1, 10.5, 9.8):
        _add_measurement(base, tid_l, date="2026-05-01", value=v,
                         lot="LOT2026A", level="L")
    for v in (51.0, 49.0, 52.0):
        _add_measurement(base, tid_h, date="2026-05-01", value=v,
                         lot="LOT2026A", level="H")

    svg = _get_svg(base, tid_l, 2026, 5)
    lxs = [float(x) for x in re.findall(
        r'<circle cx="([\d.]+)" cy="[\d.]+" r="4\.5"[^>]*><title>\[L水平\] 2026-05-01 seq=',
        svg)]
    hxs = [float(x) for x in re.findall(
        r'<polygon points="([\d.]+),[\d.]+[^"]*"[^>]*><title>\[H水平\] 2026-05-01 seq=',
        svg)]
    assert len(lxs) == 3 and len(hxs) == 3
    # 逐点配对：第 i 个 L 与第 i 个 H 同 x（按录入顺序），3 对铺开为 3 个不同横位
    assert lxs[0] == hxs[0]
    assert lxs[1] == hxs[1]
    assert lxs[2] == hxs[2]
    assert len(set(lxs)) == 3, "同日 3 对点应横向铺开为 3 个不同横位"


def test_dual_axis_point_styles(web_server):
    """六种点样式：L 绿/黄/红圆，H 蓝/黄/红三角。"""
    base, _ = web_server
    tid_l, tid_h = _setup_pair(base, project="样式测试项目", name="ALT", lot="LOT2026A")

    # 注意：两水平点全部错开日期，且警告点前后不含同向超 2SD 点，
    # 避免触发跨水平/连续 2_2s 把警告点改写为失控。
    # L：在控 10.2(+0.2SD) / 警告 12.2(+2.2SD) / 失控 13.5(+3.5SD)
    for d, v in [("2026-05-01", 10.2), ("2026-05-02", 12.2),
                 ("2026-05-03", 9.8), ("2026-05-04", 13.5)]:
        _add_measurement(base, tid_l, date=d, value=v, lot="LOT2026A", level="L")
    # H：在控 51.0(+0.2SD) / 警告 61.0(+2.2SD) / 失控 67.5(+3.5SD)
    for d, v in [("2026-05-05", 51.0), ("2026-05-06", 61.0),
                 ("2026-05-07", 48.0), ("2026-05-08", 67.5)]:
        _add_measurement(base, tid_h, date=d, value=v, lot="LOT2026A", level="H")

    svg = _get_svg(base, tid_l, 2026, 5)

    # L 圆：在控 2 点 + 图例 1 = 3；警告/失控各 1 点 + 图例 1 = 2
    for c, expect in [("#1f7a1f", 3), ("#d9a400", 2), ("#c0392b", 2)]:
        assert len(re.findall(rf'<circle[^>]*fill="{c}"', svg)) == expect, \
            f"L 圆 {c} 数量应为 {expect}"
    # H 三角：在控 2 点 + 图例 1 = 3；警告/失控各 1 点 + 图例 1 = 2
    for c, expect in [("#1f5fa3", 3), ("#d9a400", 2), ("#c0392b", 2)]:
        assert len(re.findall(rf'<polygon[^>]*fill="{c}"', svg)) == expect, \
            f"H 三角 {c} 数量应为 {expect}"
    # 图例文案
    for lbl in ["L 在控", "L 警告", "L 失控", "H 在控", "H 警告", "H 失控"]:
        assert lbl in svg, f"图例 {lbl} 应存在"
    # 提示数量（每水平 4 条测量：3 种状态 + 1 个穿插在控点）
    assert svg.count("[L水平]") == 4
    assert svg.count("[H水平]") == 4


def test_single_level_fallback(web_server):
    """验证当指标只有单个水平时，回退至单水平单图，不报错。"""
    base, _ = web_server
    pid = _post_json(base, "/api/projects", {"name": "单水平测试项目"})["id"]
    tid = _post_json(base, f"/api/projects/{pid}/test_items", {
        "name": "OnlyL", "level": "L", "lot": "LOT_SINGLE",
        "target": 25.0, "sd": 2.5,
    })["id"]
    _add_measurement(base, tid, date="2026-05-01", value=26.0,
                     lot="LOT_SINGLE", level="L")

    svg = _get_svg(base, tid, 2026, 5)
    assert "<svg" in svg
    assert "</svg>" in svg
    # 单水平：不应出现双轴图水平说明
    assert "高水平 H（右轴）" not in svg


def test_dual_axis_multi_points_same_day(web_server):
    """同日多次测定在双轴图上各自横向展开，X 轴标签带 # 次序后缀。"""
    base, _ = web_server
    tid_l, tid_h = _setup_pair(base, project="批量同日测定项目", name="VA", lot="LOT2026")

    for val_l, val_h in [(9.5, 48.0), (10.2, 51.0), (10.0, 49.5),
                         (10.8, 52.0), (9.8, 50.5)]:
        _add_measurement(base, tid_l, date="2026-04-01", value=val_l,
                         lot="LOT2026", level="L")
        _add_measurement(base, tid_h, date="2026-04-01", value=val_h,
                         lot="LOT2026", level="H")

    svg = _get_svg(base, tid_l, 2026, 4)

    # 5 个 L 点 + 5 个 H 点
    assert svg.count("[L水平]") == 5, "应包含 5 个 L 水平点"
    assert svg.count("[H水平]") == 5, "应包含 5 个 H 水平点"
    # 同水平点之间连线
    assert '<line x1=' in svg
    # 同日多条的 X 轴标签带 # 次序后缀
    assert "#" in svg


def test_dual_axis_extreme_outlier_pinned_and_lines_spread(web_server):
    """极端离群点钉边 + 标注；控制限不被压缩（±1SD 线间距 ≥ 100px）。"""
    base, _ = web_server
    tid_l, tid_h = _setup_pair(base, project="离群测试项目", name="VA", lot="LOT2026A")
    _add_measurement(base, tid_l, date="2026-10-01", value=9.9,
                     lot="LOT2026A", level="L")
    _add_measurement(base, tid_l, date="2026-10-02", value=1000.0,
                     lot="LOT2026A", level="L")  # ≈ +990SD
    _add_measurement(base, tid_h, date="2026-10-01", value=48.0,
                     lot="LOT2026A", level="H")
    _add_measurement(base, tid_h, date="2026-10-02", value=10000.0,
                     lot="LOT2026A", level="H")  # ≈ +1990SD

    svg = _get_svg(base, tid_l, 2026, 10)

    # 1. 超限点钉边 + 标注实际值（悬停保留真实数据）
    assert "↑1000.000" in svg, "L 离群点应钉在上边缘并标注 1000.000"
    assert "↑10000.000" in svg, "H 离群点应钉在上边缘并标注 10000.000"

    # 2. 控制限未被压缩：可见跨度封顶 ±4SD（图高 418px → 每 SD ≈52px），
    #    ±1SD 两线间距 ≥ 100px（旧实现被撑到 ±990SD 时 ≈0.4px）
    y1 = sorted(float(y) for y in _control_line_y_counts(svg, "#1f7a1f", "2,2"))
    y3 = sorted(float(y) for y in _control_line_y_counts(svg, "#c0392b", ""))
    assert len(y1) == 2 and len(y3) == 2, (y1, y3)
    assert y1[1] - y1[0] >= 100, f"-1SD 与 +1SD 线间距 {y1[1]-y1[0]:.1f}px 应 ≥ 100"
    assert y3[1] - y3[0] >= 300, f"-3SD 与 +3SD 线间距 {y3[1]-y3[0]:.1f}px 应 ≥ 300"

    # 3. 重合保持：每条 k·SD 线左右各画一次且 y 相同
    counts = _control_line_y_counts(svg, "#1f7a1f", "")
    assert set(counts.values()) == {2}


def test_template_card_click_shows_dual_axis_chart():
    """前端交互：检测指标卡片标题可点击直接查看 L+H 双轴图。

    用户需求 2026-10-12 第二次补充：点击检测指标即展示双轴图，
    无需先点击高/低水平子行。
    """
    from qc_tool.web import _load_template
    html = _load_template().decode("utf-8")

    # 1. 卡片标题行有可点击样式类
    assert "ti-title-row" in html, "检测指标卡片标题行应可点击"
    # 2. 双水平指标显示 L+H 双轴图角标
    assert "ti-pair-badge" in html
    assert "L+H 双轴图" in html
    # 3. 标题行 hover 提示文案
    assert "（L+H 双轴图）" in html
    # 4. 水平子行点击仍保留（用于切换录入表单默认值），不影响卡片点击
    assert "level-sub-row" in html
    assert 'e.target.closest(".level-sub-row")' in html, \
        "子行点击应阻止冒泡，卡片点击应直达双轴图"


def test_template_stats_dual_level_display():
    """前端交互：双水平指标的统计卡片同时展示 L/H 两个水平。

    用户需求 2026-10-12：本月 N / 均值 / SD / CV% / 累计 N / 累计均值
    六张统计卡片在存在同名同批号配对水平时并列显示 L=… / H=…。
    """
    from qc_tool.web import _load_template
    html = _load_template().decode("utf-8")

    # 1. loadStats 内有配对判定（同名同批号 L+H）
    assert 'siblings.find(x => x.level === "L")' in html
    assert 'siblings.find(x => x.level === "H")' in html
    # 2. 双水平分支按 L/H 归位两份统计并双色渲染
    assert "双水平：左 L（蓝）" in html
    assert "#0284c7" in html and "#db2777" in html, "L/H 统计卡片沿用蓝/品红配色"
    # 3. 六张卡片全部走 dual 展示
    for label in ["本月 N", "本月 均值", "本月 SD", "本月 CV%", "累计 N", "累计 均值"]:
        assert f'mk("{label}",' in html, f"统计卡片 {label} 应存在"
    assert 'dual(sh(dL?.month?.n), sh(dH?.month?.n))' in html
    assert 'dual(f3(dL?.cumulative?.mean), f3(dH?.cumulative?.mean))' in html
    # 4. 单水平回退分支保留
    assert 'mk("本月 CV%", d.month?.cv_pct' in html
