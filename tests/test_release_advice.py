"""
用户需求 2026-10-14：结果发放建议（质控图右侧点击面板，数据点排查信息下方）

2026-10-14 用户裁定三条规则（对失控点）：
1. 中间点（同日有前点也有后点）→ "{前}-{后} 号质控点区间内的样本结果不允许发放"
   （例：1-5 中 3 失控 → 2-4 号）
2. 当日最后点（同日有前点无后点）→ "{前}-{n} 号质控点区间内的样本结果不允许发放"
   （例：13 日 1-5 中 5 失控 → 4-5 号；次日补录 5 个点后建议不变、不产生跨日提示）
3. 当日首点 / 当日仅一点（同日无前点）→ "当日开始至 {n} 号质控点区间内的样本结果
   不允许发放"

相邻点口径：同检测指标 + 同日、跨水平不限批号、按序号找前后（序号只表示当日质控点
的顺序）；作废点不参与；在控 / 警告点返回 None（不显示）。

Run:
    python -m pytest tests/test_release_advice.py -v
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool import release_advice as RA


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "qc.db"
    init_database(db)
    c = get_connection(db)
    c.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, 'Test Lab')")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _seed_item_pair(conn: sqlite3.Connection):
    """建项目 + 同名指标 D2 的 L/H 两个 test_items（批号故意不同，验证不限批号）。"""
    p = P.create_project(conn, name="Vitamin D", short="Vit-D",
                         unit="ng/mL", manufacturer="Roche")
    ti_l = P.create_test_item(conn, p.id, name="D2", lot="LOT-L", level="L",
                              target=100.0, sd=2.0)
    ti_h = P.create_test_item(conn, p.id, name="D2", lot="LOT-H", level="H",
                              target=50.0, sd=1.0)
    return p, ti_l, ti_h


def _seed_day(conn: sqlite3.Connection, *, date: str,
              l_seqs: list[int], h_seqs: list[int],
              ooc: dict = None, pair=None) -> dict:
    """按 (level, seq) 铺同日数据；ooc={(level, seq): value} 指定失控值。

    默认 L=100（在控）、H=50（在控）；失控值给 115（+7.5SD → 1_3s）。
    pair 可传 (_seed_item_pair 返回值) 复用同一项目/指标（补录次日数据用）。
    返回 {(level, seq): measurement_id}。
    """
    ooc = ooc or {}
    p, ti_l, ti_h = pair or _seed_item_pair(conn)
    mids: dict = {}
    for seq in l_seqs:
        value = ooc.get(("L", seq), 100.0)
        m = M.create_measurement(conn, test_item_id=ti_l.id, project_id=p.id,
                                 lot="LOT-L", level="L", date=date,
                                 value=value, seq=seq)
        mids[("L", seq)] = m.id
    for seq in h_seqs:
        value = ooc.get(("H", seq), 50.0)
        m = M.create_measurement(conn, test_item_id=ti_h.id, project_id=p.id,
                                 lot="LOT-H", level="H", date=date,
                                 value=value, seq=seq)
        mids[("H", seq)] = m.id
    conn.commit()
    return mids


# ============================================================================
# compute_release_advice：纯函数
# ============================================================================

class TestComputeReleaseAdvice:
    def test_middle_point(self):
        """中间点：前点序号 至 后点序号（例：1-5 中 3 失控 → 2-4 号）。"""
        a = RA.compute_release_advice([1, 2, 3, 4, 5], [3], 3)
        assert a["case"] == "middle"
        assert (a["from"], a["to"]) == (2, 4)
        assert a["text"] == "2-4 号质控点区间内的样本结果不允许发放"

    def test_last_point_of_day(self):
        """当日最后点：前点序号 至 该点序号（例：1-5 中 5 失控 → 4-5 号）。"""
        a = RA.compute_release_advice([1, 2, 3, 4, 5], [5], 5)
        assert a["case"] == "last_of_day"
        assert (a["from"], a["to"]) == (4, 5)
        assert a["text"] == "4-5 号质控点区间内的样本结果不允许发放"

    def test_first_point_of_day(self):
        """当日首点：当日开始 至 该点序号。"""
        a = RA.compute_release_advice([1, 2, 3, 4, 5], [1], 1)
        assert a["case"] == "first_of_day"
        assert (a["from"], a["to"]) == (None, 1)
        assert a["text"] == "当日开始至 1 号质控点区间内的样本结果不允许发放"

    def test_single_point_day(self):
        """当日仅一点：同样走「当日开始至该点」。"""
        a = RA.compute_release_advice([3], [3], 3)
        assert a["case"] == "first_of_day"
        assert (a["from"], a["to"]) == (None, 3)
        assert a["text"] == "当日开始至 3 号质控点区间内的样本结果不允许发放"

    def test_seq_gap_uses_actual_neighbors(self):
        """序号不连续：用实际相邻序号（1,3,5 中 3 失控 → 1-5 号）。"""
        a = RA.compute_release_advice([1, 3, 5], [3], 3)
        assert a["case"] == "middle"
        assert (a["from"], a["to"]) == (1, 5)
        assert a["text"] == "1-5 号质控点区间内的样本结果不允许发放"

    def test_duplicates_and_unsorted_deduped(self):
        """L/H 同序号 + 乱序输入 → 去重排序后仍为 2-4 号。"""
        a = RA.compute_release_advice([5, 3, 1, 3, 2, 4, 2, 4, 5, 1], [3], 3)
        assert a["case"] == "middle"
        assert (a["from"], a["to"]) == (2, 4)

    def test_seq_not_in_seqs_returns_none(self):
        """目标序号不在序列中 / 不在失控集合中（防御分支）→ None。"""
        assert RA.compute_release_advice([1, 2, 3, 4, 5], [7], 7) is None
        assert RA.compute_release_advice([], [1], 1) is None
        assert RA.compute_release_advice([1, 2, 3], [1], 2) is None

    # ---- 连续失控点合并（2026-10-14 用户补充裁定） ----

    def test_merged_middle_run(self):
        """3、4 号连续失控：合并 2-4 与 3-5 → 当日2-5；点段内任一点结果一致。"""
        a3 = RA.compute_release_advice([1, 2, 3, 4, 5], [3, 4], 3)
        assert a3["case"] == "merged"
        assert (a3["from"], a3["to"]) == (2, 5)
        assert a3["text"] == "当日2-5质控点区间内结果不允许发放"
        a4 = RA.compute_release_advice([1, 2, 3, 4, 5], [3, 4], 4)
        assert a4 == a3

    def test_merged_run_reaches_last_point(self):
        """4、5 号连续失控（段覆盖当日最后点）→ 当日3-5。"""
        a = RA.compute_release_advice([1, 2, 3, 4, 5], [4, 5], 5)
        assert a["case"] == "merged"
        assert (a["from"], a["to"]) == (3, 5)
        assert a["text"] == "当日3-5质控点区间内结果不允许发放"

    def test_merged_run_starts_at_first_point(self):
        """1、2 号连续失控（段贴当日首点）→ 当日开始至3。"""
        a = RA.compute_release_advice([1, 2, 3, 4, 5], [1, 2], 1)
        assert a["case"] == "merged"
        assert (a["from"], a["to"]) == (None, 3)
        assert a["text"] == "当日开始至3质控点区间内结果不允许发放"

    def test_merged_whole_day_ooc(self):
        """全日全失控 → 当日开始至5。"""
        a = RA.compute_release_advice([1, 2, 3, 4, 5], [1, 2, 3, 4, 5], 3)
        assert a["case"] == "merged"
        assert (a["from"], a["to"]) == (None, 5)
        assert a["text"] == "当日开始至5质控点区间内结果不允许发放"

    def test_non_consecutive_ooc_no_merge(self):
        """3、5 号失控但 4 号在控：不合并，各自按单点规则提示。"""
        a3 = RA.compute_release_advice([1, 2, 3, 4, 5], [3, 5], 3)
        assert a3["case"] == "middle"
        assert a3["text"] == "2-4 号质控点区间内的样本结果不允许发放"
        a5 = RA.compute_release_advice([1, 2, 3, 4, 5], [3, 5], 5)
        assert a5["case"] == "last_of_day"
        assert a5["text"] == "4-5 号质控点区间内的样本结果不允许发放"

    def test_merged_across_seq_gap_in_list(self):
        """序号有缺（1,2,4,5）且 2、4 失控：在有序列表上相邻 → 合并当日1-5。"""
        a = RA.compute_release_advice([1, 2, 4, 5], [2, 4], 2)
        assert a["case"] == "merged"
        assert (a["from"], a["to"]) == (1, 5)
        assert a["text"] == "当日1-5质控点区间内结果不允许发放"


# ============================================================================
# build_release_advice：DB 层（同指标 + 同日、跨水平不限批号、排除作废）
# ============================================================================

class TestBuildReleaseAdvice:
    def test_middle_point_across_levels(self, conn):
        """L-3 失控且两水平同日 1-5 齐全 → 2-4 号区间。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5],
                         ooc={("L", 3): 115.0})
        a = RA.build_release_advice(conn, mids[("L", 3)])
        assert a["case"] == "middle"
        assert a["text"] == "2-4 号质控点区间内的样本结果不允许发放"

    def test_last_point_stays_after_next_day_upload(self, conn):
        """13 日 5 号失控 → 4-5 号；次日补录 5 个点后建议不变、不产生跨日提示。"""
        pair = _seed_item_pair(conn)
        mids = _seed_day(conn, date="2026-10-13",
                         l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5],
                         ooc={("L", 5): 115.0}, pair=pair)
        a1 = RA.build_release_advice(conn, mids[("L", 5)])
        assert a1["case"] == "last_of_day"
        assert a1["text"] == "4-5 号质控点区间内的样本结果不允许发放"
        # 次日 10-14 又上传 5 个质控点（两水平各 5 条，均在控）
        _seed_day(conn, date="2026-10-14",
                  l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5], pair=pair)
        a2 = RA.build_release_advice(conn, mids[("L", 5)])
        assert a2["case"] == "last_of_day"
        assert a2["text"] == "4-5 号质控点区间内的样本结果不允许发放", \
            "次日补录后建议必须保持不变、不得产生跨日提示"

    def test_first_point_of_day(self, conn):
        """当日首点失控 → 当日开始至该点。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3], h_seqs=[1, 2, 3],
                         ooc={("L", 1): 115.0})
        a = RA.build_release_advice(conn, mids[("L", 1)])
        assert a["case"] == "first_of_day"
        assert a["text"] == "当日开始至 1 号质控点区间内的样本结果不允许发放"

    def test_single_point_day(self, conn):
        """当日仅一条且失控 → 当日开始至该点。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1], h_seqs=[], ooc={("L", 1): 115.0})
        a = RA.build_release_advice(conn, mids[("L", 1)])
        assert a["case"] == "first_of_day"
        assert a["text"] == "当日开始至 1 号质控点区间内的样本结果不允许发放"

    def test_cross_level_neighbor_when_own_level_missing(self, conn):
        """L 只有 1-3、H 有 1-4：L-3 失控时后点取跨水平的 4 → 2-4 号（不是当日最后点）。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3], h_seqs=[1, 2, 3, 4],
                         ooc={("L", 3): 115.0})
        a = RA.build_release_advice(conn, mids[("L", 3)])
        assert a["case"] == "middle"
        assert a["text"] == "2-4 号质控点区间内的样本结果不允许发放"

    def test_void_neighbor_skipped(self, conn):
        """作废点不参与相邻点序列：序号 2（L/H 均作废）后 3 失控 → 1-4 号。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3, 4], h_seqs=[1, 2, 3, 4],
                         ooc={("L", 3): 115.0})
        M.mark_void(conn, mids[("L", 2)])
        M.mark_void(conn, mids[("H", 2)])
        conn.commit()
        a = RA.build_release_advice(conn, mids[("L", 3)])
        assert a["case"] == "middle"
        assert a["text"] == "1-4 号质控点区间内的样本结果不允许发放"

    def test_seq_gap_uses_actual_neighbors_db(self, conn):
        """同日序号不连续（1,3,5）：3 失控 → 1-5 号。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 3, 5], h_seqs=[1, 3, 5],
                         ooc={("L", 3): 115.0})
        a = RA.build_release_advice(conn, mids[("L", 3)])
        assert a["case"] == "middle"
        assert a["text"] == "1-5 号质控点区间内的样本结果不允许发放"

    def test_in_control_returns_none(self, conn):
        """在控点 → None（不显示建议）。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3], h_seqs=[1, 2, 3])
        assert RA.build_release_advice(conn, mids[("L", 1)]) is None

    def test_warning_returns_none(self, conn):
        """1_2s 警告点 → None（仅失控点显示建议）。"""
        p, ti_l, ti_h = _seed_item_pair(conn)
        m = M.create_measurement(conn, test_item_id=ti_l.id, project_id=p.id,
                                 lot="LOT-L", level="L", date="2026-04-01",
                                 value=104.5, seq=1)  # +2.25SD → 警告
        conn.commit()
        assert RA.build_release_advice(conn, m.id) is None

    def test_void_point_returns_none(self, conn):
        """失控点被标记作废后 → None。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3], h_seqs=[1, 2, 3],
                         ooc={("L", 2): 115.0})
        M.mark_void(conn, mids[("L", 2)])
        conn.commit()
        assert RA.build_release_advice(conn, mids[("L", 2)]) is None

    def test_missing_measurement_returns_none(self, conn):
        """measurement_id 不存在 → None。"""
        assert RA.build_release_advice(conn, 999999) is None

    # ---- 连续失控点合并（DB 层） ----

    def test_merged_consecutive_ooc_db(self, conn):
        """L-3、L-4 连续失控：点 3 与点 4 都返回合并区间 当日2-5。"""
        mids = _seed_day(conn, date="2026-10-03",
                         l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5],
                         ooc={("L", 3): 115.0, ("L", 4): 115.0})
        a3 = RA.build_release_advice(conn, mids[("L", 3)])
        assert a3["case"] == "merged"
        assert a3["text"] == "当日2-5质控点区间内结果不允许发放"
        a4 = RA.build_release_advice(conn, mids[("L", 4)])
        assert a4["text"] == "当日2-5质控点区间内结果不允许发放"

    def test_merged_run_reaches_last_db(self, conn):
        """13 日 4、5 号失控 → 当日3-5；次日补录后合并建议同样不变、不跨日。"""
        pair = _seed_item_pair(conn)
        mids = _seed_day(conn, date="2026-10-13",
                         l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5],
                         ooc={("L", 4): 115.0, ("L", 5): 115.0}, pair=pair)
        a1 = RA.build_release_advice(conn, mids[("L", 5)])
        assert a1["case"] == "merged"
        assert a1["text"] == "当日3-5质控点区间内结果不允许发放"
        _seed_day(conn, date="2026-10-14",
                  l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5], pair=pair)
        a2 = RA.build_release_advice(conn, mids[("L", 5)])
        assert a2["text"] == "当日3-5质控点区间内结果不允许发放", \
            "次日补录后合并建议必须不变、不得跨日"

    def test_non_consecutive_ooc_no_merge_db(self, conn):
        """3、5 号失控、4 号在控：不合并。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5],
                         ooc={("L", 3): 115.0, ("L", 5): 115.0})
        a3 = RA.build_release_advice(conn, mids[("L", 3)])
        assert a3["case"] == "middle"
        assert a3["text"] == "2-4 号质控点区间内的样本结果不允许发放"
        a5 = RA.build_release_advice(conn, mids[("L", 5)])
        assert a5["case"] == "last_of_day"
        assert a5["text"] == "4-5 号质控点区间内的样本结果不允许发放"

    def test_cross_level_ooc_merges_by_seq(self, conn):
        """L-3 失控 + H-4 失控（L-4 在控）：序号层面连续 → 合并当日2-5。"""
        mids = _seed_day(conn, date="2026-04-01",
                         l_seqs=[1, 2, 3, 4, 5], h_seqs=[1, 2, 3, 4, 5],
                         ooc={("L", 3): 115.0, ("H", 4): 58.0})
        a = RA.build_release_advice(conn, mids[("L", 3)])
        assert a["case"] == "merged"
        assert a["text"] == "当日2-5质控点区间内结果不允许发放"
