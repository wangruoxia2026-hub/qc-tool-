"""
Issue #3（功能 C：统计量自动计算）—— 测试先行

覆盖 spec §C.1–C.3 全部验收标准 a–g + §9 边界 + §C.2 独立性验证。

运行：
    python -m pytest tests/test_issue3_stats.py -v
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool import statistics as S


# ============================================================================
# Fixture
# ============================================================================

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


def _seed_full(
    conn: sqlite3.Connection,
    *,
    project: str = "维生素 D",
    name: str = "D2",
    lot: str = "LOT1",
    target: float = 100.0,
    sd: float = 2.0,
    created_at: str = "2026-01-01T00:00:00",
) -> dict:
    """建项目 + L/H 水平，返 id 字典。"""
    p = P.create_project(conn, name=project)
    ti_l = P.create_test_item(
        conn, p.id, name=name, lot=lot, level="L",
        target=target, sd=sd,
    )
    # 手动把 created_at 改到指定日期（方便测累计统计）
    conn.execute(
        "UPDATE test_items SET created_at = ? WHERE id = ?",
        (created_at, ti_l.id),
    )
    ti_h = P.create_test_item(
        conn, p.id, name=name, lot=lot, level="H",
        target=target, sd=sd,
    )
    conn.execute(
        "UPDATE test_items SET created_at = ? WHERE id = ?",
        (created_at, ti_h.id),
    )
    conn.commit()
    return {
        "project_id": p.id,
        "test_item_id_l": ti_l.id,
        "test_item_id_h": ti_h.id,
    }


def _seed_meas(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    project_id: int,
    lot: str = "LOT1",
    level: str = "L",
    date: str,
    value: float,
) -> None:
    """录一条 measurement。"""
    M.create_measurement(
        conn,
        test_item_id=test_item_id,
        project_id=project_id,
        lot=lot, level=level,
        date=date, value=value,
    )


# ============================================================================
# §C.1 a + §C.1 c 边界：纯函数 N/均值/SD/CV%
# ============================================================================

class TestStatsPure:
    """纯函数 compute_stats_from_values：SD 公式、CV%、N=0/1/全相同"""

    def test_normal(self):
        """5 个数据点，正常统计。"""
        stats = S.compute_stats_from_values([98, 100, 102, 99, 101])
        assert stats.n == 5
        assert stats.mean is not None
        assert stats.sd is not None
        assert stats.cv_pct is not None
        # mean ≈ 100
        assert abs(stats.mean - 100.0) < 0.1

    def test_N0_returns_empty(self):
        """N=0 → empty()"""
        stats = S.compute_stats_from_values([])
        assert stats.n == 0
        assert stats.mean is None
        assert stats.sd is None
        assert stats.cv_pct is None

    def test_N1_returns_one(self):
        """N=1 → one(value)，sd/cv 为 None"""
        stats = S.compute_stats_from_values([105.0])
        assert stats.n == 1
        assert stats.mean == 105.0
        assert stats.sd is None
        assert stats.cv_pct is None

    def test_all_same_sd0_cv_none(self):
        """数据全部相同 → SD=0，CV%='—'(None)"""
        stats = S.compute_stats_from_values([100.0, 100.0, 100.0])
        assert stats.n == 3
        assert stats.mean == 100.0
        assert stats.sd == 0.0
        assert stats.cv_pct is None

    def test_cv_pct_formula(self):
        """CV% = sd/mean × 100"""
        # mean=100, sd=2 → CV%=2%
        stats = S.compute_stats_from_values([98, 100, 102, 99, 101])
        # sd ≈ 1.581, mean ≈ 100
        assert stats.cv_pct is not None
        assert abs(stats.cv_pct - (stats.sd / stats.mean * 100)) < 0.001

    def test_mean_rounded_to_3dp(self):
        """均值/SD/CV% 取整到 3 位小数（spec §D.5）"""
        stats = S.compute_stats_from_values([100.0000001, 100.0000002])
        assert len(str(stats.mean).rstrip('0').split('.')[-1]) <= 3

    def test_sample_std_dev_n_minus_1(self):
        """样本标准差：分母 n-1（IUPAC/CNAS-GL027 共识）"""
        # [2, 4] → mean=3, 平方和=(1+1)=2, n-1=1 → sd=sqrt(2)=1.414
        stats = S.compute_stats_from_values([2.0, 4.0])
        assert abs(stats.sd - (2.0 ** 0.5)) < 0.001


# ============================================================================
# §C.1 b + §C.1 d：source='固定' 时统计区只显示靶值/SD/CV%
# ============================================================================

class TestSourceFixed:
    """source='固定'：统计区只显示录入的靶值与 SD 和 CV%"""

    def test_fixed_shows_target_and_sd(self, fresh_db):
        """source='固定' → 统计区显示手填靶值/SD，不走历史计算。"""
        ids = _seed_full(fresh_db, target=100.0, sd=2.0)
        view = S.get_statistics_view(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            source="固定",
            today=date(2026, 4, 15),
        )
        # 固定模式：无论库里有多少数据，mean/sd 都显示手填值
        assert view["month"].mean == 100.0
        assert view["month"].sd == 2.0
        assert view["month"].cv_pct == 2.0  # 2/100*100

    def test_fixed_cumulative_also_target(self, fresh_db):
        """source='固定' → 累计统计也显示靶值（不受历史数据影响）。"""
        ids = _seed_full(fresh_db, target=100.0, sd=2.0)
        # 录一些数据
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                    project_id=ids["project_id"],
                    date="2026-03-15", value=110.0)
        view = S.get_statistics_view(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            source="固定",
            today=date(2026, 4, 15),
        )
        # 累计栏也显示手填值，不受 110.0 影响
        assert view["cumulative"].mean == 100.0
        assert view["cumulative"].sd == 2.0

    def test_fixed_n_reflects_actual_data(self, fresh_db):
        """source='固定' → N 显示实际数据点数（而非∞）。"""
        ids = _seed_full(fresh_db)
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                    project_id=ids["project_id"], date="2026-04-01", value=100.0)
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                    project_id=ids["project_id"], date="2026-04-02", value=101.0)
        view = S.get_statistics_view(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            source="固定",
            today=date(2026, 4, 15),
        )
        assert view["month"].n == 2


# ============================================================================
# §C.1 b：source='累计' 时按历史重算
# ============================================================================

class TestSourceCumulative:
    """source='累计'：均值/SD 按历史重算"""

    def test_cumulative_recalculates(self, fresh_db):
        """source='累计' → 累计统计按历史计算（不等同靶值）。"""
        ids = _seed_full(fresh_db, target=100.0, sd=2.0,
                         created_at="2026-01-01T00:00:00")
        # 录 3 条历史均值=105，sd=1
        for v in [104.0, 105.0, 106.0]:
            _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                       project_id=ids["project_id"], date="2026-03-01", value=v)
        view = S.get_statistics_view(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            source="累计",
            today=date(2026, 4, 15),
        )
        # 累计均值 ≈ 105，不等于靶值 100
        assert view["cumulative"].mean is not None
        assert abs(view["cumulative"].mean - 105.0) < 0.1
        # 累计 sd ≠ 2.0（手填值）
        assert view["cumulative"].sd != 2.0

    def test_cumulative_switch_changes_display(self, fresh_db):
        """切换 source → 统计区显示立即变化。"""
        ids = _seed_full(fresh_db, target=100.0, sd=2.0)
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-03-01", value=110.0)
        view_fixed = S.get_statistics_view(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L", source="固定",
            today=date(2026, 4, 15),
        )
        view_cum = S.get_statistics_view(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L", source="累计",
            today=date(2026, 4, 15),
        )
        # 两种模式的累计统计不相等
        assert view_fixed["cumulative"].mean != view_cum["cumulative"].mean


# ============================================================================
# §C.1 c：N<2 时 CV% 显示"—"
# ============================================================================

class TestCVDash:
    """少于 2 个数据点时 CV% 显示"—"(None)"""

    def test_n1_cv_none(self):
        stats = S.compute_stats_from_values([100.0])
        assert stats.n == 1
        assert stats.cv_pct is None

    def test_n2_cv_calculated(self):
        stats = S.compute_stats_from_values([98.0, 102.0])
        assert stats.n == 2
        assert stats.cv_pct is not None

    def test_n0_cv_none(self):
        stats = S.compute_stats_from_values([])
        assert stats.n == 0
        assert stats.cv_pct is None


# ============================================================================
# §C.1 d：统计页显示本月/累计 N/均值/SD/CV%
# ============================================================================

class TestMonthScope:
    """本月统计区间 = 当前自然月内数据"""

    def test_month_scope_current_month_only(self, fresh_db):
        """2026-04-15 查询：3 月数据不算本月，4 月算。"""
        ids = _seed_full(fresh_db, created_at="2026-01-01T00:00:00")
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-03-31", value=100.0)
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-04-01", value=101.0)
        stats = S.compute_month_stats(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            today=date(2026, 4, 15),
        )
        assert stats.n == 1
        assert abs(stats.mean - 101.0) < 0.1


class TestCumulativeScope:
    """累计统计区间 = 建立日 ~ 上一自然月最后一天"""

    def test_cum_scope_stops_at_prev_month(self, fresh_db):
        """4 月查：累计止于 3 月底，不含 4 月。"""
        ids = _seed_full(fresh_db, created_at="2026-01-01T00:00:00")
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-03-31", value=99.0)
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-04-01", value=101.0)
        stats = S.compute_cumulative_stats(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            today=date(2026, 4, 15),
        )
        # 4/1 不计入累计
        assert stats.n == 1
        assert abs(stats.mean - 99.0) < 0.1

    def test_cum_scope_empty_when_created_after_prev_month(self, fresh_db):
        """4 月新建的检测指标：建立日 > 上一月最后一天 → N=0。"""
        ids = _seed_full(fresh_db, created_at="2026-04-01T00:00:00")
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-04-10", value=100.0)
        stats = S.compute_cumulative_stats(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            today=date(2026, 4, 15),
        )
        assert stats.n == 0
        assert stats.mean is None


# ============================================================================
# §C.1 e：source 默认 '固定'
# ============================================================================

class TestSourceDefaultFixed:
    """test_items.source 创建时默认 '固定'"""

    def test_source_default_is_fixed(self, fresh_db):
        """新建检测指标 source='固定'"""
        ids = _seed_full(fresh_db)  # create_test_item 用默认 source='固定'
        row = fresh_db.execute(
            "SELECT source FROM test_items WHERE id = ?",
            (ids["test_item_id_l"],)
        ).fetchone()
        assert row["source"] == "固定"

    def test_source_can_be_switched_to_cumulative(self, fresh_db):
        """用户可在检测指标配置中切换为 '累计'（update_test_item）"""
        ids = _seed_full(fresh_db)
        P.update_test_item(fresh_db, ids["test_item_id_l"], source="累计")
        row = fresh_db.execute(
            "SELECT source FROM test_items WHERE id = ?",
            (ids["test_item_id_l"],)
        ).fetchone()
        assert row["source"] == "累计"


# ============================================================================
# §C.1 f：切换 source 只影响统计区显示，不影响 Westgard 判定
# ============================================================================

class TestSourceDoesNotAffectWestgard:
    """source 切换与 Westgard 无关（纯展示层）"""

    def test_switching_source_does_not_recalculate_westgard(self, fresh_db):
        """录一条 ±3SD 数据 → 状态是失控 → 切 source → 状态不变。"""
        ids = _seed_full(fresh_db, target=100.0, sd=2.0)
        # 录 107.0 → |sd| = 3.5 → 1_3s 失控
        m = M.create_measurement(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=107.0,
        )
        assert m.status is not None and m.status.startswith("失控")
        # 切 source
        P.update_test_item(fresh_db, ids["test_item_id_l"], source="累计")
        # Westgard 状态不变（不变）
        refreshed = M.get_measurement(fresh_db, m.id)
        assert refreshed.status == m.status
        # 再切回
        P.update_test_item(fresh_db, ids["test_item_id_l"], source="固定")
        refreshed2 = M.get_measurement(fresh_db, m.id)
        assert refreshed2.status == m.status


# ============================================================================
# §C.2 + §C.1 g：Westgard 控制限固定用靶值+SD，改靶值仅影响新数据
# ============================================================================

class TestWestgardControlLimitsFixed:
    """Westgard 控制限始终使用检测指标配置的靶值 + SD"""

    def test_westgard_uses_configured_target_sd(self, fresh_db):
        """录一条 +3.5SD 的值 → 无论 source 是固定还是累计，都触发 1_3s。"""
        ids = _seed_full(fresh_db, target=100.0, sd=2.0)
        # source='累计'
        P.update_test_item(fresh_db, ids["test_item_id_l"], source="累计")
        m = M.create_measurement(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=107.0,
        )
        assert m.status is not None and "1_3s" in m.status


# ============================================================================
# §9 边界：作废行不参与统计
# ============================================================================

class TestVoidExcluded:
    """作废行（status='作废'）不参与统计"""

    def test_void_excluded_from_month_stats(self, fresh_db):
        """本月统计不含作废行。"""
        ids = _seed_full(fresh_db)
        m = M.create_measurement(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=110.0,
        )
        M.mark_void(fresh_db, m.id)
        stats = S.compute_month_stats(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            today=date(2026, 4, 15),
        )
        assert stats.n == 0

    def test_void_excluded_from_cumulative_stats(self, fresh_db):
        """累计统计不含作废行。"""
        ids = _seed_full(fresh_db, created_at="2026-01-01T00:00:00")
        m = M.create_measurement(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-03-01", value=110.0,
        )
        M.mark_void(fresh_db, m.id)
        stats = S.compute_cumulative_stats(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            today=date(2026, 4, 15),
        )
        assert stats.n == 0


# ============================================================================
# §C.1 d：统计页两栏独立
# ============================================================================

class TestTwoColumnsIndependent:
    """本月统计与累计统计是两条独立数据"""

    def test_month_and_cumulative_can_differ(self, fresh_db):
        """两栏均值/SD 可以不同。"""
        ids = _seed_full(fresh_db, target=100.0, sd=2.0,
                         created_at="2026-01-01T00:00:00")
        # 3 月累计数据均值=105
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-03-01", value=105.0)
        # 4 月数据均值=98
        _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                   project_id=ids["project_id"], date="2026-04-01", value=98.0)
        view = S.get_statistics_view(
            fresh_db,
            test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            source="累计",
            today=date(2026, 4, 15),
        )
        # 两栏均值不同
        assert view["month"].mean != view["cumulative"].mean
        # 本月 N=1
        assert view["month"].n == 1
        # 累计 N=1（不含 4 月）
        assert view["cumulative"].n == 1


# ============================================================================
# §9 边界：N=0 → "无数据"（mean=None 即 UI 显"—"）
# ============================================================================

class TestNoDataBoundary:
    """N=0 → Stats.empty() → UI 显示"—"""

    def test_empty_stats_returns_none_fields(self):
        stats = S.Stats.empty()
        assert stats.n == 0
        assert stats.mean is None
        assert stats.sd is None
        assert stats.cv_pct is None

    def test_no_data_month(self, fresh_db):
        """本月无数据 → Stats.empty()"""
        ids = _seed_full(fresh_db, created_at="2026-01-01T00:00:00")
        stats = S.compute_month_stats(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            today=date(2026, 4, 15),
        )
        assert stats.n == 0


# ============================================================================
# §9 边界：数据全部相同 → SD=0, CV%="—"
# ============================================================================

class TestAllSameBoundary:
    """数据全部相同 → SD=0，CV% 为 None"""

    def test_all_same_sd0_cv_none(self):
        stats = S.compute_stats_from_values([50.0, 50.0, 50.0, 50.0])
        assert stats.sd == 0.0
        assert stats.cv_pct is None
        assert stats.mean == 50.0
        assert stats.n == 4


# ============================================================================
# §C.1 e 验收：source 切换入口存在
# ============================================================================

class TestSourceSwitchable:
    """用户可在检测指标配置中切换 source"""

    def test_update_source_to_cumulative(self, fresh_db):
        """update_test_item 支持 source 字段"""
        ids = _seed_full(fresh_db)
        updated = P.update_test_item(
            fresh_db, ids["test_item_id_l"], source="累计"
        )
        assert updated.source == "累计"

    def test_update_source_back_to_fixed(self, fresh_db):
        """source 可切回 '固定'"""
        ids = _seed_full(fresh_db)
        P.update_test_item(fresh_db, ids["test_item_id_l"], source="累计")
        updated = P.update_test_item(
            fresh_db, ids["test_item_id_l"], source="固定"
        )
        assert updated.source == "固定"


# ============================================================================
# 快照：固定/累计 模式下统计区行为差异
# ============================================================================

class TestSnapshotFixedVsCumulative:
    """快照：source='固定' vs '累计' 的统计区差异"""

    def test_fixed_no_history_calculation(self, fresh_db):
        """固定模式：统计区永远不按历史数据算 mean/sd"""
        ids = _seed_full(fresh_db, target=200.0, sd=4.0,
                         created_at="2026-01-01T00:00:00")
        # 录一堆均值 150 的历史数据
        for v in [149, 150, 151, 149, 150]:
            _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                       project_id=ids["project_id"], date="2026-03-15", value=v)
        view = S.get_statistics_view(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            source="固定",
            today=date(2026, 4, 15),
        )
        # 累计栏显示靶值，不受历史影响
        assert view["cumulative"].mean == 200.0
        assert view["cumulative"].sd == 4.0
        assert view["cumulative"].cv_pct == 2.0  # 4/200*100

    def test_cumulative_uses_history_mean(self, fresh_db):
        """累计模式：累计栏按历史计算均值"""
        ids = _seed_full(fresh_db, target=200.0, sd=4.0,
                         created_at="2026-01-01T00:00:00")
        for v in [149, 150, 151, 149, 150]:
            _seed_meas(fresh_db, test_item_id=ids["test_item_id_l"],
                       project_id=ids["project_id"], date="2026-03-15", value=v)
        view = S.get_statistics_view(
            fresh_db, test_item_id=ids["test_item_id_l"],
            lot="LOT1", level="L",
            source="累计",
            today=date(2026, 4, 15),
        )
        # 累计栏均值 ≈ 150，不等于靶值 200
        # 注：[149,150,151,149,150] 样本均值=149.8（浮点）
        assert abs(view["cumulative"].mean - 150.0) < 0.21
