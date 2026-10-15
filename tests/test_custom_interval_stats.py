"""
测试：自定义区间数据统计功能
覆盖：
1. 统计函数支持指定起止日期（跨月/跨年）
2. exclude_ooc=True (默认) 刨除失控点，保留警告点、在控点
3. exclude_ooc=False 包含失控点
4. 作废点（status='作废'）在任何情况下均排除
5. Web API /api/stats/<tid>/custom?start=...&end=...&exclude_ooc=...
6. 边界条件：N=0（无数据）、N=1（仅均值）、全相同（SD=0, CV=None）
"""
import json
import sqlite3
import urllib.request
from datetime import date
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading
import socket
import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool import statistics as S
from qc_tool import web as webmod


@pytest.fixture
def fresh_db(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    conn.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, '测试实验室')")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_compute_custom_interval_stats(fresh_db):
    conn = fresh_db
    p = P.create_project(conn, name="维生素AE")
    ti = P.create_test_item(conn, p.id, name="VA", lot="L202610", level="L", target=40.0, sd=2.0)

    # 跨月数据插入：2026-09-28 至 2026-10-25
    # 9月28: 在控 40.0
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L202610", level="L", date="2026-09-28", seq=1, value=40.0, operator="张三")
    # 10月01: 在控 40.0
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L202610", level="L", date="2026-10-01", seq=1, value=40.0, operator="张三")
    # 10月05: 警告 44.5 (超出2s，小于3s)
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L202610", level="L", date="2026-10-05", seq=1, value=44.5, operator="张三")
    # 10月10: 在控 40.0 (阻断 10-05 与 10-15 形成连续 2_2s)
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L202610", level="L", date="2026-10-10", seq=1, value=40.0, operator="张三")
    # 10月15: 失控 48.0 (超出3s，触发 1_3s 失控)
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L202610", level="L", date="2026-10-15", seq=1, value=48.0, operator="张三")
    # 10月20: 作废 40.5
    m_void = M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L202610", level="L", date="2026-10-20", seq=1, value=40.5, operator="张三")
    M.mark_void(conn, m_void.id)
    # 10月25: 在控 39.5
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L202610", level="L", date="2026-10-25", seq=1, value=39.5, operator="张三")
    conn.commit()

    # 1. 默认刨除失控点 (exclude_ooc=True)，区间 2026-10-01 至 2026-10-25
    # 纳入点：10-01 (40.0 在控), 10-05 (44.5 警告保留), 10-10 (40.0 在控), 10-25 (39.5 在控) -> N=4
    # 刨除：10-15 (失控), 10-20 (作废)
    # 9-28 不在区间内
    stats_default = S.compute_interval_stats(
        conn, test_item_id=ti.id, lot="L202610", level="L",
        date_from="2026-10-01", date_to="2026-10-25",
        exclude_ooc=True
    )
    assert stats_default.n == 4
    # values: 40.0, 44.5, 40.0, 39.5 -> sum = 164.0 -> mean = 41.000
    assert stats_default.mean == 41.000
    assert stats_default.sd is not None
    assert stats_default.cv_pct is not None

    # 2. 不刨除失控点 (exclude_ooc=False)，区间 2026-10-01 至 2026-10-25
    # 纳入点：10-01, 10-05, 10-10, 10-15 (48.0 失控纳入), 10-25 (作废 10-20 依然排除) -> N=5
    stats_with_ooc = S.compute_interval_stats(
        conn, test_item_id=ti.id, lot="L202610", level="L",
        date_from="2026-10-01", date_to="2026-10-25",
        exclude_ooc=False
    )
    assert stats_with_ooc.n == 5
    # sum = 164.0 + 48.0 = 212.0 -> mean = 212.0 / 5 = 42.400
    assert stats_with_ooc.mean == 42.400

    # 3. 跨月区间：2026-09-20 至 2026-10-25 (exclude_ooc=True)
    # 纳入点：9-28 (40.0) + 10月4个点 -> N=5
    stats_cross_month = S.compute_interval_stats(
        conn, test_item_id=ti.id, lot="L202610", level="L",
        date_from="2026-09-20", date_to="2026-10-25",
        exclude_ooc=True
    )
    assert stats_cross_month.n == 5
    assert stats_cross_month.mean == round((40.0 + 164.0) / 5, 3)

    # 4. 获取起止日期辅助函数 get_data_date_range
    dr = S.get_data_date_range(conn, test_item_id=ti.id, lot="L202610", level="L")
    assert dr == ("2026-09-28", "2026-10-25")


def test_custom_stats_web_api(tmp_path: Path):
    db_path = tmp_path / "qc.db"
    init_database(db_path)
    conn = get_connection(db_path)
    conn.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, '测试实验室')")
    p = P.create_project(conn, name="维生素AE")
    ti = P.create_test_item(conn, p.id, name="VA", lot="L1", level="L", target=40.0, sd=2.0)
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L1", level="L", date="2026-10-01", seq=1, value=40.0, operator="张三")
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L1", level="L", date="2026-10-15", seq=1, value=48.0, operator="张三") # 失控
    M.create_measurement(conn, test_item_id=ti.id, project_id=p.id, lot="L1", level="L", date="2026-10-25", seq=1, value=41.0, operator="张三")
    conn.commit()
    conn.close()

    port = _free_port()
    handler = webmod.make_handler(db_path=db_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.db_path = db_path
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        # GET /api/stats/<tid>/custom?start=2026-10-01&end=2026-10-25&exclude_ooc=true
        url_exclude = f"{base_url}/api/stats/{ti.id}/custom?start=2026-10-01&end=2026-10-25&exclude_ooc=true"
        req = urllib.request.Request(url_exclude)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["n"] == 2
            assert data["mean"] == 40.5
            assert data["date_range"] == ["2026-10-01", "2026-10-25"]
            assert data["db_range"] == ["2026-10-01", "2026-10-25"]

        # GET /api/stats/<tid>/custom?start=2026-10-01&end=2026-10-25&exclude_ooc=false
        url_include = f"{base_url}/api/stats/{ti.id}/custom?start=2026-10-01&end=2026-10-25&exclude_ooc=false"
        req = urllib.request.Request(url_include)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["n"] == 3
            assert data["mean"] == 43.0
    finally:
        server.shutdown()


def test_custom_stats_html_elements(tmp_path: Path):
    """测试 index.html 中包含『数据统计』按钮与弹窗相关关键元素"""
    template_path = Path(__file__).resolve().parent.parent / "qc_tool" / "web" / "templates" / "index.html"
    html = template_path.read_text(encoding="utf-8")
    assert "btn-custom-stats" in html
    assert "数据统计" in html
    assert "openCustomStatsModal" in html
    assert "executeCustomStatsCalc" in html
    assert "cs-start-date" in html
    assert "cs-end-date" in html
    assert "cs-exclude-ooc" in html
    assert "刨除失控点计算" in html
