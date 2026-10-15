"""
用户需求 2026-10-14：结果发放建议（Web）

覆盖：
1. GET /api/ooc/<mid>/release-advice：
   - 失控中间点 → "2-4 号质控点区间内的样本结果不允许发放"
   - 当日最后点 → "4-5 号..."；次日补录后建议不变（不产生跨日提示）
   - 当日首点 → "当日开始至 1 号..."
   - 在控 / 警告 / 不存在 → advice=null
   - 非数字 id → 404
2. HTML 模板静态扫描：点击面板失控分支含"结果发放建议"区块 + loadReleaseAdvice

Run:
    python -m pytest tests/test_web_release_advice.py -v
"""
from __future__ import annotations

import json
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


def _get_advice(base: str, mid) -> dict:
    return json.loads(urlreq.urlopen(
        f"{base}/api/ooc/{mid}/release-advice", timeout=5
    ).read().decode("utf-8"))


def _get_status(base: str, path: str) -> int:
    try:
        urlreq.urlopen(f"{base}{path}", timeout=5)
        return 200
    except Exception as e:
        return getattr(e, "code", 500)


def _seed(db_path: Path, *, date: str, ooc_seqs: list[int] = (),
          warning: bool = False, pair=None):
    """直接落库：项目 + D2 指标 L/H（批号不同）+ 该日 1-5 号两水平数据。

    ooc_seqs → L 这些序号给 115.0（+7.5SD 失控）；warning → 单条 +2.25SD 警告点。
    pair 传上一次返回的 (p_id, ti_l_id, ti_h_id) 可复用项目/指标（补录次日数据用）。
    返回 ({(level, seq): mid}, pair)。
    """
    from qc_tool.db import init_database, get_connection
    from qc_tool import projects as P
    from qc_tool import measurements as M

    init_database(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO lab_settings(id, lab_name) VALUES (1, 'Test Lab')")
    if pair is None:
        p = P.create_project(conn, name="Vitamin D", short="Vit-D",
                             unit="ng/mL", manufacturer="Roche")
        ti_l = P.create_test_item(conn, p.id, name="D2", lot="LOT-L", level="L",
                                  target=100.0, sd=2.0)
        ti_h = P.create_test_item(conn, p.id, name="D2", lot="LOT-H", level="H",
                                  target=50.0, sd=1.0)
        pair = (p.id, ti_l.id, ti_h.id)
    p_id, ti_l_id, ti_h_id = pair

    mids = {}
    for seq in range(1, 6):
        v = 115.0 if seq in ooc_seqs else 100.0
        m = M.create_measurement(conn, test_item_id=ti_l_id, project_id=p_id,
                                 lot="LOT-L", level="L", date=date,
                                 value=v, seq=seq)
        mids[("L", seq)] = m.id
        m = M.create_measurement(conn, test_item_id=ti_h_id, project_id=p_id,
                                 lot="LOT-H", level="H", date=date,
                                 value=50.0, seq=seq)
        mids[("H", seq)] = m.id
    if warning:
        m = M.create_measurement(conn, test_item_id=ti_l_id, project_id=p_id,
                                 lot="LOT-L", level="L", date=date,
                                 value=104.5, seq=6)  # +2.25SD → 1_2s 警告
        mids[("L", 6)] = m.id
    conn.commit()
    conn.close()
    return mids, pair


class TestReleaseAdviceAPI:
    def test_middle_point(self, web_server):
        base, tmp = web_server
        mids, _ = _seed(tmp / "qc.db", date="2026-04-01", ooc_seqs=[3])
        body = _get_advice(base, mids[("L", 3)])
        assert body["measurement_id"] == mids[("L", 3)]
        assert body["advice"]["case"] == "middle"
        assert body["advice"]["text"] == "2-4 号质控点区间内的样本结果不允许发放"

    def test_last_point_unchanged_after_next_day_upload(self, web_server):
        """13 日 5 号失控 → 4-5 号；14 日补录 5 个点后接口返回不变。"""
        base, tmp = web_server
        mids, pair = _seed(tmp / "qc.db", date="2026-10-13", ooc_seqs=[5])
        a1 = _get_advice(base, mids[("L", 5)])["advice"]
        assert a1["case"] == "last_of_day"
        assert a1["text"] == "4-5 号质控点区间内的样本结果不允许发放"
        # 次日补录（同项目同指标，两水平各 5 条均在控）
        _seed(tmp / "qc.db", date="2026-10-14", pair=pair)
        a2 = _get_advice(base, mids[("L", 5)])["advice"]
        assert a2["text"] == "4-5 号质控点区间内的样本结果不允许发放", \
            "次日补录后建议必须不变、不得跨日"

    def test_first_point_of_day(self, web_server):
        base, tmp = web_server
        mids, _ = _seed(tmp / "qc.db", date="2026-04-01", ooc_seqs=[1])
        body = _get_advice(base, mids[("L", 1)])
        assert body["advice"]["case"] == "first_of_day"
        assert body["advice"]["text"] == \
            "当日开始至 1 号质控点区间内的样本结果不允许发放"

    def test_merged_consecutive_ooc(self, web_server):
        """3、4 号连续失控：点 3 / 点 4 接口都返回合并区间 当日2-5。"""
        base, tmp = web_server
        mids, _ = _seed(tmp / "qc.db", date="2026-10-03", ooc_seqs=[3, 4])
        a3 = _get_advice(base, mids[("L", 3)])["advice"]
        assert a3["case"] == "merged"
        assert a3["text"] == "当日2-5质控点区间内结果不允许发放"
        a4 = _get_advice(base, mids[("L", 4)])["advice"]
        assert a4["text"] == "当日2-5质控点区间内结果不允许发放"

    def test_merged_unchanged_after_next_day_upload(self, web_server):
        """13 日 4、5 号失控 → 当日3-5；14 日补录后合并建议不变。"""
        base, tmp = web_server
        mids, pair = _seed(tmp / "qc.db", date="2026-10-13", ooc_seqs=[4, 5])
        a1 = _get_advice(base, mids[("L", 5)])["advice"]
        assert a1["case"] == "merged"
        assert a1["text"] == "当日3-5质控点区间内结果不允许发放"
        _seed(tmp / "qc.db", date="2026-10-14", pair=pair)
        a2 = _get_advice(base, mids[("L", 5)])["advice"]
        assert a2["text"] == "当日3-5质控点区间内结果不允许发放", \
            "次日补录后合并建议必须不变、不得跨日"

    def test_in_control_returns_null(self, web_server):
        base, tmp = web_server
        mids, _ = _seed(tmp / "qc.db", date="2026-04-01")
        body = _get_advice(base, mids[("L", 2)])
        assert body["advice"] is None

    def test_warning_returns_null(self, web_server):
        base, tmp = web_server
        mids, _ = _seed(tmp / "qc.db", date="2026-04-01", warning=True)
        body = _get_advice(base, mids[("L", 6)])
        assert body["advice"] is None

    def test_missing_measurement_returns_null(self, web_server):
        base, _ = web_server
        body = _get_advice(base, 999999)
        assert body["measurement_id"] == 999999
        assert body["advice"] is None

    def test_non_numeric_id_returns_404(self, web_server):
        base, _ = web_server
        assert _get_status(base, "/api/ooc/abc/release-advice") == 404


# ============================================================================
# HTML 模板静态扫描
# ============================================================================

def _load_html() -> str:
    from qc_tool.web import _load_template
    return _load_template().decode("utf-8")


def test_html_panel_has_release_advice_block():
    """点击面板失控分支：排查信息下方有结果发放建议区块 + loadReleaseAdvice。"""
    html = _load_html()
    assert 'id="point-panel-release-box"' in html, "缺结果发放建议容器"
    assert 'id="point-panel-release"' in html, "缺建议文案容器"
    assert "结果发放建议" in html
    assert "loadReleaseAdvice" in html
    assert "/api/ooc/${mid}/release-advice" in html or \
        "release-advice" in html, "建议应走后端 release-advice 接口"
