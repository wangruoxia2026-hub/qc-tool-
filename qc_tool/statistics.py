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
5. 2026-10-13 用户裁定（spec 附录 B.5）：
   - 统计区两栏始终按数据实算，不受 test_items.source（固定/累计）影响；
   - 本月统计 = 本月内全部数据（含失控/警告点，只刨作废）；
   - 累计统计 = 第一条质控数据日期 ~ 上一自然月最后一天，
     刨除失控点（警告点保留、作废排除）；
   - 累计时间区间**不让用户调**（spec §10 第 11 条 + §C.1）。
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
    exclude_ooc: bool = False,
) -> list[float]:
    """从 measurements 表取一段日期区间内的 values（排除作废）。

    SQL：按 (test_item_id, lot, level, date) 过滤，date BETWEEN date_from AND date_to_inclusive，
    作废行（status='作废'）不参与统计。

    exclude_ooc=True（2026-10-13 用户裁定"刨除失控点"）：
    用白名单保留 NULL / '' / 在控 / InControl / 警告* / Warning* 的行，
    其余状态（失控* / OutOfControl* 等）全部排除；警告点保留参与统计。
    """
    sql = ("SELECT value FROM measurements "
           "WHERE test_item_id = ? AND lot = ? AND level = ? "
           "  AND date >= ? AND date <= ? "
           "  AND (status IS NULL OR status != '作废') ")
    if exclude_ooc:
        sql += ("AND (status IS NULL OR status = '' "
                "OR status = '在控' OR status = 'InControl' "
                "OR status LIKE '警告%' OR status LIKE 'Warning%') ")
    sql += "ORDER BY date, seq, id"
    rows = conn.execute(
        sql,
        (test_item_id, lot, level, date_from, date_to_inclusive),
    ).fetchall()
    return [float(r["value"]) for r in rows]


def first_data_date(
    conn: sqlite3.Connection,
    test_item_id: int,
    lot: str,
    level: str,
) -> Optional[str]:
    """第一条质控数据的日期（2026-10-13 用户裁定：累计统计起点 = 首条数据日）。

    返回 'YYYY-MM-DD' 或 None（该指标+批号+水平没有任何非作废数据）。
    """
    row = conn.execute(
        "SELECT MIN(date) FROM measurements "
        "WHERE test_item_id = ? AND lot = ? AND level = ? "
        "  AND (status IS NULL OR status != '作废')",
        (test_item_id, lot, level),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def get_data_date_range(
    conn: sqlite3.Connection,
    test_item_id: int,
    lot: str,
    level: str,
) -> tuple[Optional[str], Optional[str]]:
    """获取当前指标+批号+水平在库质控数据的起止时间 (MIN(date), MAX(date))。

    用于自定义统计弹窗自动带入区间。排除作废数据。
    """
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM measurements "
        "WHERE test_item_id = ? AND lot = ? AND level = ? "
        "  AND (status IS NULL OR status != '作废')",
        (test_item_id, lot, level),
    ).fetchone()
    if row is None or row[0] is None:
        return (None, None)
    return (str(row[0]), str(row[1]))


def compute_interval_stats(
    conn: sqlite3.Connection,
    test_item_id: int,
    lot: str,
    level: str,
    *,
    date_from: str,
    date_to: str,
    exclude_ooc: bool = True,
) -> Stats:
    """计算指定日期区间内的质控数据统计量（N / Mean / SD / CV%）。

    - 支持任意起止日期（可跨月、跨年）；
    - exclude_ooc=True (默认)：刨除失控点，保留警告点、在控点；
    - exclude_ooc=False：包含失控点；
    - 作废点始终排除；
    - 边界：N=0 返回 empty()，N=1 返回 one()，全相同 SD=0.0，CV%=None。
    """
    vals = _fetch_values(
        conn,
        test_item_id=test_item_id,
        lot=lot,
        level=level,
        date_from=date_from,
        date_to_inclusive=date_to,
        exclude_ooc=exclude_ooc,
    )
    return compute_stats_from_values(vals)


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
    exclude_ooc: bool = True,
) -> Stats:
    """累计统计（2026-10-13 用户裁定）：第一条质控数据日期 ~ 上一自然月最后一天。

    起 = 该指标 + 批号 + 水平的第一条质控数据的日期（不是 test_items.created_at）
    止 = 上一自然月最后一天（不计入当前自然月）
    默认刨除失控点（用户裁定"刨除失控点计算"；警告点保留、作废行始终排除）；
    月报 sheet2 需要"含失控"版本时显式传 exclude_ooc=False。
    """
    today = today or date.today()
    start_s = first_data_date(conn, test_item_id, lot, level)
    if start_s is None:
        return Stats.empty()

    end = _last_day_of_prev_month(today)
    start_d = datetime.strptime(start_s, "%Y-%m-%d").date()
    if start_d > end:
        # 首条数据 > 上一自然月最后一天 → 时间区间为空（N=0）
        return Stats.empty()

    vals = _fetch_values(
        conn, test_item_id=test_item_id, lot=lot, level=level,
        date_from=start_d.isoformat(), date_to_inclusive=end.isoformat(),
        exclude_ooc=exclude_ooc,
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
    source: str = "固定",
    today: Optional[date] = None,
) -> dict:
    """统计区两栏展示数据。

    2026-10-13 用户裁定：统计区始终按数据实算，不受 test_items.source
    （固定/累计）影响——source 只用于新建指标时确定靶值/SD。
    `source` 参数仅向后兼容保留（不再影响结果）。

    - 本月统计：当前自然月内全部数据（刨作废，含失控/警告点）
    - 累计统计：第一条质控数据日期 ~ 上一自然月最后一天，
      刨除失控点（警告点保留、作废排除），N/均值/SD/CV% 全算

    返回：
        {
            "month":     Stats,   # 本月统计
            "cumulative":Stats,   # 累计统计
        }
    """
    today = today or date.today()
    month_stats = compute_month_stats(
        conn, test_item_id=test_item_id, lot=lot, level=level, today=today,
    )
    cum_stats = compute_cumulative_stats(
        conn, test_item_id=test_item_id, lot=lot, level=level,
        today=today, exclude_ooc=True,
    )
    return {"month": month_stats, "cumulative": cum_stats}


__all__ = [
    "Stats",
    "ROUND_PRECISION",
    "compute_stats_from_values",
    "compute_month_stats",
    "compute_cumulative_stats",
    "compute_interval_stats",
    "get_statistics_view",
    "first_data_date",
    "get_data_date_range",
]
