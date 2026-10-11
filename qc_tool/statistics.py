"""
功能 C：统计量自动计算（spec V1.3 §C.1–C.3）

对外 API
--------
- Stats                              # 数据类
- compute_stats_from_values          # 纯函数（N/mean/sd/cv%）
- compute_month_stats                # 本月统计（当前自然月内）
- compute_cumulative_stats           # 累计统计（建立日 ~ 上一自然月最后一天）
- get_statistics_view                # 按 source 取「本月 + 累计」两栏展示数据

设计要点（spec V1.3 §C.1–C.3 + AGENTS.md 铁律）：
1. "等于"控制限不算超出：CV% = sd / mean × 100，但**全部用 round(., 3) 截断**以与 §D.5 一致。
2. SD 公式：样本标准差 n−1（IUPAC / CNAS-GL027 共识，临床 Westgard 默认）。
3. 边界（N=0 / N=1 / 全相同）：
   - N=0：mean/sd/cv=None
   - N=1：mean=单值，sd/cv=None
   - 全相同：sd=0.0，cv=None
4. 作废行不参与统计（与 §B.4 d 一致，调用方在 SQL 阶段已过滤）。
5. 累计时间区间**不让用户调**（spec §10 第 11 条 + §C.1）。
   起 = test_items.created_at 的日期部分；
   止 = 当前自然月第一天的前一天（即上一自然月最后一天）；
   当累计时间区间为 0 或负 → 当作"无数据"。
6. 不读 Westgard / 不写 measurements / out_of_control —— 本模块纯展示统计。
"""

from __future__ import annotations

import sqlite3
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional, Sequence


# spec §D.5：浮点取整到 3 位小数后比较；CV% 也按 3 位小数输出，与"等同控制限不算超出"一致
ROUND_PRECISION = 3


# ============================================================================
# 数据类
# ============================================================================

@dataclass
class Stats:
    """统计区的一行数据（spec §C.1 显示 N / 均值 / SD / CV%）。

    字段任一为 None → UI 端按 spec §C.1 / §9 边界显示 "—" 或 "无数据"（N=0）。
    sd=0.0 → CV% 必为 None（避免除零，spec §C.1 / §9 边界）。
    """
    n: int
    mean: Optional[float]
    sd: Optional[float]
    cv_pct: Optional[float]

    @classmethod
    def empty(cls) -> "Stats":
        """N=0 → 整行"无数据"。"""
        return cls(n=0, mean=None, sd=None, cv_pct=None)

    @classmethod
    def one(cls, value: float) -> "Stats":
        """N=1 → 只有均值，sd/cv 不可计算。"""
        return cls(n=1, mean=round(float(value), ROUND_PRECISION), sd=None, cv_pct=None)

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "mean": self.mean,
            "sd": self.sd,
            "cv_pct": self.cv_pct,
        }


# ============================================================================
# 纯函数
# ============================================================================

def compute_stats_from_values(values: Iterable[float]) -> Stats:
    """纯计算：给定一组测定值 → 返回 N/均值/SD/CV%。

    - N=0 → Stats.empty()
    - N=1 → Stats.one(value)
    - 全相同 → sd=0.0, cv_pct=None
    - SD 公式：sqrt(sum((xi-mean)^2) / (N-1))   样本标准差
    - CV%  = sd / mean * 100
    - 所有数值 round 到 3 位小数（与 spec §D.5 浮点等于判断一致）

    入参允许 list / tuple / 任何可迭代；空迭代 → empty()。
    """
    vals: list[float] = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return Stats.empty()
    if n == 1:
        return Stats.one(vals[0])

    mean_raw = sum(vals) / n
    # 样本标准差（n−1）；n≥2 时分母 ≥ 1
    sq = sum((v - mean_raw) ** 2 for v in vals)
    sd_raw = (sq / (n - 1)) ** 0.5

    mean = round(mean_raw, ROUND_PRECISION)
    sd = round(sd_raw, ROUND_PRECISION)
    if sd == 0.0 or mean == 0.0:
        # 全相同 / 均值为 0 → SD=0 或除零 → CV% 不计算
        return Stats(n=n, mean=mean, sd=sd, cv_pct=None)
    cv_pct_raw = (sd / mean) * 100.0
    cv_pct = round(cv_pct_raw, ROUND_PRECISION)
    return Stats(n=n, mean=mean, sd=sd, cv_pct=cv_pct)


# ============================================================================
# 时间区间工具
# ============================================================================

def _first_day_of_month(d: date) -> date:
    """返回当月 1 号。"""
    return d.replace(day=1)


def _last_day_of_month(d: date) -> date:
    """返回当月最后一天（calendar.monthrange）。"""
    last = monthrange(d.year, d.month)[1]
    return d.replace(day=last)


def _last_day_of_prev_month(today: date) -> date:
    """返回当前自然月第一天的前一天 = 上一自然月最后一天。"""
    first_this_month = _first_day_of_month(today)
    # 上一月最后一天 = (本月 1 号 - 1 天)
    from datetime import timedelta
    return first_this_month - timedelta(days=1)


def _parse_iso_date(s: str) -> date:
    """解析 'YYYY-MM-DD' 或 'YYYY-MM-DDTHH:MM:SS'（test_items.created_at 形式）。"""
    s = s[:10]  # 只取日期部分
    return datetime.strptime(s, "%Y-%m-%d").date()


# ============================================================================
# 区间查 values（含按月 + 按批号 + 水平 + 作废过滤）
# ============================================================================

def _fetch_values(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    lot: str,
    level: str,
    date_from: str,
    date_to_inclusive: str,
) -> list[float]:
    """从 measurements 表取一段日期区间内的 values（排除作废）。

    SQL：按 (test_item_id, lot, level, date) 过滤，date BETWEEN date_from AND date_to_inclusive，
    作废行（status='作废'）不参与统计。
    """
    rows = conn.execute(
        "SELECT value FROM measurements "
        "WHERE test_item_id = ? AND lot = ? AND level = ? "
        "  AND date >= ? AND date <= ? "
        "  AND (status IS NULL OR status != '作废') "
        "ORDER BY date, seq, id",
        (test_item_id, lot, level, date_from, date_to_inclusive),
    ).fetchall()
    return [float(r["value"]) for r in rows]


def compute_month_stats(
    conn: sqlite3.Connection,
    test_item_id: int,
    lot: str,
    level: str,
    *,
    today: Optional[date] = None,
) -> Stats:
    """本月统计：当前自然月内的数据（spec §C.1 d）。

    date_from = today.replace(day=1)
    date_to   = today 当月最后一天
    """
    today = today or date.today()
    first = _first_day_of_month(today)
    last = _last_day_of_month(today)
    vals = _fetch_values(
        conn, test_item_id=test_item_id, lot=lot, level=level,
        date_from=first.isoformat(), date_to_inclusive=last.isoformat(),
    )
    return compute_stats_from_values(vals)


def compute_cumulative_stats(
    conn: sqlite3.Connection,
    test_item_id: int,
    lot: str,
    level: str,
    *,
    today: Optional[date] = None,
) -> Stats:
    """累计统计：检测指标建立日 ~ 上一自然月最后一天（spec §C.1 d）。

    起 = MAX(test_items.created_at 日期部分, 上一自然月最后一天的次日？)  
       实际：起 = test_items.created_at 的日期部分（spec §C.1 d 明确）
    止 = 上一自然月最后一天
    不计入当前自然月（spec §C.1 d 明确）
    """
    today = today or date.today()
    ti_row = conn.execute(
        "SELECT created_at FROM test_items WHERE id = ?", (test_item_id,)
    ).fetchone()
    if ti_row is None or ti_row["created_at"] is None:
        return Stats.empty()
    try:
        created_d = _parse_iso_date(ti_row["created_at"])
    except ValueError:
        return Stats.empty()

    end = _last_day_of_prev_month(today)
    if created_d > end:
        # 建立日 > 上一自然月最后一天 → 时间区间为空（spec §C.1 / §9 边界：N=0）
        return Stats.empty()

    vals = _fetch_values(
        conn, test_item_id=test_item_id, lot=lot, level=level,
        date_from=created_d.isoformat(), date_to_inclusive=end.isoformat(),
    )
    return compute_stats_from_values(vals)


# ============================================================================
# 综合入口：按 source 返回「本月 + 累计」两栏展示数据
# ============================================================================

def get_statistics_view(
    conn: sqlite3.Connection,
    test_item_id: int,
    lot: str,
    level: str,
    *,
    source: str,
    today: Optional[date] = None,
) -> dict:
    """spec §C.1：返回两栏展示数据，按 source 切换。

    source='固定'：mean/sd 来自 test_items.target/sd（手填值），cv_pct=sd/target*100
    source='累计'：mean/sd 来自历史（按 §C.1 d 区间重算）
    本月统计：在 source='固定' 下也按"本月内的数据"算 N（spec §C.1 d 明确"本月统计"区间），
             但当 source='固定' 时 mean/sd/cv 仍用 target/sd 显示。

    返回：
        {
            "month":     Stats,   # 本月统计
            "cumulative":Stats,   # 累计统计
        }
    """
    today = today or date.today()
    if source not in ("固定", "累计"):
        raise ValueError(f"source 必须是 '固定' 或 '累计'，当前: {source!r}")

    # 1. 区间查值（用于 source='累计' 的均值/SD；source='固定' 仅用于 N）
    first_this = _first_day_of_month(today)
    last_this = _last_day_of_month(today)
    end_prev = _last_day_of_prev_month(today)

    # 本月数据
    month_vals = _fetch_values(
        conn, test_item_id=test_item_id, lot=lot, level=level,
        date_from=first_this.isoformat(), date_to_inclusive=last_this.isoformat(),
    )
    # 累计数据（建立日 ~ 上一自然月最后一天）
    ti_row = conn.execute(
        "SELECT created_at, target, sd FROM test_items WHERE id = ?",
        (test_item_id,),
    ).fetchone()
    if ti_row is None:
        return {
            "month": Stats.empty(),
            "cumulative": Stats.empty(),
        }

    cum_vals: list[float] = []
    if ti_row["created_at"] is not None:
        try:
            created_d = _parse_iso_date(ti_row["created_at"])
            if created_d <= end_prev:
                cum_vals = _fetch_values(
                    conn, test_item_id=test_item_id, lot=lot, level=level,
                    date_from=created_d.isoformat(), date_to_inclusive=end_prev.isoformat(),
                )
        except ValueError:
            cum_vals = []

    # 2. 组装两栏
    if source == "固定":
        target = float(ti_row["target"])
        sd_cfg = float(ti_row["sd"])
        # 固定模式下：mean/sd 来自手填；N 来自实际区间（spec §C.1 d 明确）
        # 固定模式：N=0 时仍显示靶值/sd 作为参考（spec §C.1 b 明确"显示配置的靶值与 SD"）
        mean_f = round(target, ROUND_PRECISION)
        sd_f = round(sd_cfg, ROUND_PRECISION)
        if sd_f == 0.0 or mean_f == 0.0:
            cv_f: Optional[float] = None
        else:
            cv_f = round((sd_f / mean_f) * 100.0, ROUND_PRECISION)
        month_stats = Stats(n=len(month_vals), mean=mean_f, sd=sd_f, cv_pct=cv_f)
        # 累计栏：N=0 时也显示靶值/sd 作为参考（固定模式原则一致）
        cum_stats = Stats(n=len(cum_vals), mean=mean_f, sd=sd_f, cv_pct=cv_f)
    else:
        # source='累计'：mean/sd 来自历史
        month_stats = compute_stats_from_values(month_vals)
        cum_stats = compute_stats_from_values(cum_vals)

    return {"month": month_stats, "cumulative": cum_stats}


__all__ = [
    "Stats",
    "ROUND_PRECISION",
    "compute_stats_from_values",
    "compute_month_stats",
    "compute_cumulative_stats",
    "get_statistics_view",
]
