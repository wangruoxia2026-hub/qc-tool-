"""
结果发放建议（用户需求 2026-10-14）

质控图右侧点击面板中、数据点排查信息下方，对「失控点」给出不允许发放结果的
序号区间建议。2026-10-14 用户裁定规则：

单点失控（三条）：
1. 中间点（同日有前点也有后点）
   → "{前}-{后} 号质控点区间内的样本结果不允许发放"（例：1-5 中 3 失控 → 2-4 号）
2. 当日最后点（同日有前点、无后点）
   → "{前}-{n} 号质控点区间内的样本结果不允许发放"（例：13 日 1-5 中 5 失控 → 4-5 号；
     次日补录数据后建议保持不变，区间只在同日闭合，绝不跨日）
3. 当日首点 / 当日仅一点（同日无前点）
   → "当日开始至 {n} 号质控点区间内的样本结果不允许发放"

连续失控点合并（2026-10-14 用户补充裁定）：
把当日失控点聚成最大连续段（当日有序序号列表上相邻、中间无非失控点隔断；
某序号 L/H 任一水平失控即算该序号失控）。点击段内任意失控点 → 显示整段合并区间，
自然扩展到边界（用户裁定）：
- 段前后都有点 → "当日{a}-{b}质控点区间内结果不允许发放"（a=段首前点、b=段尾后点；
  例：3、4 号失控 → 当日2-5）
- 段覆盖当日最后点 → "当日{a}-{n}质控点区间内结果不允许发放"（a=段首前点、
  n=段尾即最后失控点；例：4、5 号失控 → 当日3-5）
- 段贴当日首点 / 全日全失控 → "当日开始至{b}质控点区间内结果不允许发放"
  （b=段尾后点，无后点则为段尾；例：1、2 号失控 → 当日开始至3）

口径（用户 2026-10-14 裁定）：
- 相邻点 = 同检测指标（project+name）+ 同日、两水平合并、不限批号，按序号找前后；
  序号只表示当日质控点的顺序，不附加批次语义（L/H 同序号自动去重）。
- 作废点不参与相邻点序列。
- 在控 / 警告(1_2s) 点 → None（不显示建议区块）。
- 纯展示：不落库、不改变判定状态。

公开 API：
- compute_release_advice(seqs, ooc_seqs, seq)  # 纯函数：同日非作废序号 + 失控序号 + 目标序号
- build_release_advice(conn, measurement_id)    # DB 层：查点 + 同日相邻序号 → 建议
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .lj_chart import _ChartStatus, _parse_status


def compute_release_advice(
    seqs: list[int],
    ooc_seqs: list[int],
    seq: int,
) -> Optional[dict[str, Any]]:
    """纯函数：按同日序号序列给出结果发放区间建议（含连续失控段合并）。

    - seqs：同日同检测指标、非作废点的序号集合（可重复、乱序；L/H 同序号去重）
    - ooc_seqs：其中失控的序号（任意水平失控即算该序号失控）
    - seq：被点击失控点的序号
    - seq 不在 seqs / ooc_seqs 中（防御分支）→ None
    """
    ordered = sorted({int(s) for s in seqs})
    ooc = {int(s) for s in ooc_seqs}
    target = int(seq)
    if target not in ordered or target not in ooc:
        return None

    # 包含 target 的最大连续失控段（在 ordered 上相邻、无非失控点隔断）
    i = ordered.index(target)
    lo_idx = i
    while lo_idx > 0 and ordered[lo_idx - 1] in ooc:
        lo_idx -= 1
    hi_idx = i
    while hi_idx < len(ordered) - 1 and ordered[hi_idx + 1] in ooc:
        hi_idx += 1
    run = ordered[lo_idx:hi_idx + 1]
    first, last = run[0], run[-1]

    # 单点失控：原有三情形
    if len(run) == 1:
        prev: Optional[int] = None
        nxt: Optional[int] = None
        for s in ordered:
            if s < target:
                prev = s
            elif s > target:
                nxt = s
                break
        if prev is not None and nxt is not None:
            case, lo, hi = "middle", prev, nxt
        elif prev is not None:
            case, lo, hi = "last_of_day", prev, target
        else:
            case, lo, hi = "first_of_day", None, target
        if case == "first_of_day":
            text = f"当日开始至 {hi} 号质控点区间内的样本结果不允许发放"
        else:
            text = f"{lo}-{hi} 号质控点区间内的样本结果不允许发放"
        return {"case": case, "from": lo, "to": hi, "text": text}

    # 连续失控段：合并区间（自然扩展到边界）
    prev = None
    nxt = None
    for s in ordered:
        if s < first:
            prev = s
        elif s > last:
            nxt = s
            break
    if prev is not None and nxt is not None:
        lo, hi = prev, nxt
        text = f"当日{lo}-{hi}质控点区间内结果不允许发放"
    elif prev is not None:
        lo, hi = prev, last
        text = f"当日{lo}-{hi}质控点区间内结果不允许发放"
    else:
        lo, hi = None, (nxt if nxt is not None else last)
        text = f"当日开始至{hi}质控点区间内结果不允许发放"
    return {"case": "merged", "from": lo, "to": hi, "text": text}


def build_release_advice(
    conn: sqlite3.Connection,
    measurement_id: int,
) -> Optional[dict[str, Any]]:
    """查该 measurement 是否失控；失控 → 按同日（跨水平、不限批号）序号序列给建议。

    返回 None 的情形：measurement 不存在 / 非失控（在控、警告）/ 已作废。
    """
    row = conn.execute(
        "SELECT id, test_item_id, project_id, lot, level, date, seq, status "
        "FROM measurements WHERE id = ?",
        (measurement_id,),
    ).fetchone()
    if row is None:
        return None
    st = _parse_status(row["status"])
    if st != _ChartStatus.OUT_OF_CONTROL:
        return None  # 在控 / 警告 / 作废（VOID）均不显示

    # 同检测指标（project+name，L/H 两行同 name）+ 同日，不限批号、不限水平
    day_rows = conn.execute(
        "SELECT m.seq, m.status FROM measurements m "
        "JOIN test_items t ON t.id = m.test_item_id "
        "WHERE t.project_id = ? "
        "  AND t.name = (SELECT name FROM test_items WHERE id = ?) "
        "  AND m.date = ?",
        (row["project_id"], row["test_item_id"], row["date"]),
    ).fetchall()
    seqs = [
        int(r["seq"]) for r in day_rows
        if _parse_status(r["status"]) != _ChartStatus.VOID
    ]
    ooc_seqs = [
        int(r["seq"]) for r in day_rows
        if _parse_status(r["status"]) == _ChartStatus.OUT_OF_CONTROL
    ]
    return compute_release_advice(seqs, ooc_seqs, int(row["seq"]))


__all__ = ["compute_release_advice", "build_release_advice"]
