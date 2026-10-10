"""
Westgard 多规则质控判定核心

按 spec §D 严格执行：
- 判定单元: 检测指标 + 批号 + 水平 + 日期（四维）
- 规则检索顺序: 1_3s → 2_2s → R_4s → 4_1s → 10_x
- 1_2s 是独立的警告标记，不是失控前提
- "等于"控制限不算超出（浮点取整到 3 位小数后比较）

验收标准（spec §D.4）：
a. |value| = 2.01SD → 警告(1_2s) 黄色
b. |value| = 3.01SD → 失控(1_3s) 红色
c. 同水平连续 2 条 |value| > 2SD 同方向 → 后一条失控(2_2s)
d. 跨水平同日都 |value| > 2SD 同方向 → 当日两条均失控(2_2s)
e. 同一测定日期一个 +2SD 另一个 −2SD → 两条均失控(R_4s)
f. 同水平连续 4 个 |value| > 1SD 同方向 → 第 4 条失控(4_1s)
g. 跨水平两水平各连续 2 次同方向 |value| > 1SD → 第 4 条失控(4_1s)
h. 累计 10 个同侧值 → 第 10 条失控(10_x)
i. 4 个超 ±1SD 但不超 ±2SD → 触发 4_1s，不触发 1_2s
"""
from __future__ import annotations
from enum import Enum
from typing import List, Dict, Any, Optional


# ============== 常量 ==============

# spec D.5: 浮点取整到 3 位小数后比较
ROUND_PRECISION = 3


class Status(str, Enum):
    """质控状态"""
    IN_CONTROL = "in_control"  # 在控
    WARNING = "warning"  # 警告（黄色）
    OUT_OF_CONTROL = "out_of_control"  # 失控（红色）


# 失控规则列表（按判定顺序）
OUT_OF_CONTROL_RULES = ["1_3s", "2_2s", "R_4s", "4_1s", "10_x"]

# 所有规则
RULES = ["1_2s", "1_3s", "2_2s", "R_4s", "4_1s", "10_x"]

# 控制限
LIMIT_1SD = 1
LIMIT_2SD = 2
LIMIT_3SD = 3
LIMIT_4SD = 4

# 阈值
THRESHOLD_1SD = 1.0  # |value| > 1SD (取整后严格 > 1.000)
THRESHOLD_2SD = 2.0  # |value| > 2SD (取整后严格 > 2.000)
THRESHOLD_3SD = 3.0  # |value| > 3SD (取整后严格 > 3.000)
THRESHOLD_4SD = 4.0  # 极差 >= 4SD (取整后 >= 4.000)


# ============== 基础工具 ==============

def sd_score(value: float, target: float, sd: float) -> float:
    """
    计算 SD 分数（value 与 target 的偏差，单位 SD）
    返回未取整的浮点数；调用方应使用 round(..., 3) 后比较
    """
    if sd == 0:
        raise ValueError("SD 不能为 0")
    return (value - target) / sd


def is_exceeded(score: float, threshold: float) -> bool:
    """
    判断是否"超出"控制限（取整到 3 位小数后严格 > 阈值）
    等于阈值视为不超出（spec D.5）
    """
    rounded = round(score, ROUND_PRECISION)
    return rounded > threshold


def is_reached(score: float, threshold: float) -> bool:
    """
    判断是否"达到"阈值（取整到 3 位小数后 >= 阈值）
    用于 4SD 极差判断（极差 = 4SD 算失控）
    """
    rounded = round(score, ROUND_PRECISION)
    return rounded >= threshold


# ============== 单条判定规则 ==============

def check_1_2s(value: float, target: float, sd: float) -> bool:
    """
    警告规则 1_2s: |value| > 2SD
    仅警告，不算失控
    """
    score = sd_score(value, target, sd)
    return is_exceeded(abs(score), THRESHOLD_2SD)


def check_1_3s(value: float, target: float, sd: float) -> bool:
    """
    失控规则 1_3s: |value| > 3SD
    """
    score = sd_score(value, target, sd)
    return is_exceeded(abs(score), THRESHOLD_3SD)


def check_2_2s(
    history: List[float],
    target: float,
    sd: float,
    cross_value: Optional[float] = None,
    cross_target: Optional[float] = None,
    cross_sd: Optional[float] = None,
) -> bool:
    """
    失控规则 2_2s: 连续 2 个质控值同方向超 ±2SD

    两种触发场景：
    1. 同水平连续 2 条超 2SD 同方向
    2. 跨水平同日：当前水平最后 1 条 + 另一水平当日 1 条，都超 2SD 同方向

    参数:
        history: 该水平的历史值列表（按时间顺序，不含当前 value）
        target: 当前水平靶值
        sd: 当前水平 SD
        cross_value: 另一水平当日值（None 表示无跨水平数据）
        cross_target: 另一水平靶值
        cross_sd: 另一水平 SD
    """
    # 场景 1: 同水平连续 2 条
    if len(history) >= 2:
        last2 = history[-2:]
        dirs = []
        for v in last2:
            score = sd_score(v, target, sd)
            if is_exceeded(abs(score), THRESHOLD_2SD):
                dirs.append(1 if score > 0 else -1)
            else:
                dirs = None
                break
        if dirs is not None and dirs[0] == dirs[1]:
            return True

    # 场景 2: 跨水平同日（当前水平 1 条 + 另一水平 1 条）
    if (cross_value is not None and cross_target is not None
            and cross_sd is not None and len(history) >= 1):
        # 当前水平最后 1 条
        last1 = history[-1]
        cur_score = sd_score(last1, target, sd)
        if not is_exceeded(abs(cur_score), THRESHOLD_2SD):
            return False
        cur_dir = 1 if cur_score > 0 else -1
        # 另一水平当日值
        cross_score = sd_score(cross_value, cross_target, cross_sd)
        if not is_exceeded(abs(cross_score), THRESHOLD_2SD):
            return False
        cross_dir = 1 if cross_score > 0 else -1
        return cur_dir == cross_dir

    return False


def check_r_4s(
    low_value: Optional[float], low_target: float, low_sd: float,
    high_value: Optional[float], high_target: float, high_sd: float,
) -> bool:
    """
    失控规则 R_4s: 同一测定日期内两水平极差 >= 4SD
    一个 +2SD 另一个 -2SD → 极差 = 4SD → 触发
    """
    if low_value is None or high_value is None:
        return False
    low_score = sd_score(low_value, low_target, low_sd)
    high_score = sd_score(high_value, high_target, high_sd)
    # 极差 = |low_score - high_score|
    range_score = abs(low_score - high_score)
    return is_reached(range_score, THRESHOLD_4SD)


def check_4_1s(history: List[float], target: float, sd: float) -> bool:
    """
    失控规则 4_1s: 同水平连续 4 个 |value| > 1SD 同方向
    history 是该水平的历史数据（按时间顺序），最后 4 条均超 1SD 且同方向 → 触发
    """
    if len(history) < 4:
        return False
    last4 = history[-4:]
    dirs = []
    for v in last4:
        score = sd_score(v, target, sd)
        if is_exceeded(abs(score), THRESHOLD_1SD):
            dirs.append(1 if score > 0 else -1)
        else:
            return False
    return all(d == dirs[0] for d in dirs)


def check_10_x(history: List[float], target: float, sd: float) -> bool:
    """
    失控规则 10_x: 累计 10 个同侧值（同一水平或跨水平）
    全部在 target 同侧（无需超 1SD）
    """
    if len(history) < 10:
        return False
    last10 = history[-10:]
    scores = [sd_score(v, target, sd) for v in last10]
    # 同侧：全部 > 0 或全部 < 0
    return all(s > 0 for s in scores) or all(s < 0 for s in scores)


# ============== 跨水平规则 ==============

def check_4_1s_cross_level(
    low_history: List[float], low_target: float, low_sd: float,
    high_history: List[float], high_target: float, high_sd: float,
) -> bool:
    """
    跨水平 4_1s: 两水平各连续 ≥ 2 次同方向 |value| > 1SD → 触发
    验收 g
    """
    if len(low_history) < 2 or len(high_history) < 2:
        return False
    # 取低水平最近 2 条
    low_last2 = low_history[-2:]
    high_last2 = high_history[-2:]
    # 判定低水平方向
    low_dirs = []
    for v in low_last2:
        score = sd_score(v, low_target, low_sd)
        if is_exceeded(abs(score), THRESHOLD_1SD):
            low_dirs.append(1 if score > 0 else -1)
        else:
            return False
    # 判定高水平方向
    high_dirs = []
    for v in high_last2:
        score = sd_score(v, high_target, high_sd)
        if is_exceeded(abs(score), THRESHOLD_1SD):
            high_dirs.append(1 if score > 0 else -1)
        else:
            return False
    # 两水平同方向
    return (all(d > 0 for d in low_dirs) and all(d > 0 for d in high_dirs)) or \
           (all(d < 0 for d in low_dirs) and all(d < 0 for d in high_dirs))


def check_10_x_cross_level(
    low_history: List[float], low_target: float, low_sd: float,
    high_history: List[float], high_target: float, high_sd: float,
) -> bool:
    """
    跨水平 10_x: 两水平各连续 ≥ 5 次同方向 → 触发
    """
    if len(low_history) < 5 or len(high_history) < 5:
        return False
    low_last5 = low_history[-5:]
    high_last5 = high_history[-5:]
    # 低水平同方向
    low_scores = [sd_score(v, low_target, low_sd) for v in low_last5]
    high_scores = [sd_score(v, high_target, high_sd) for v in high_last5]
    low_all_pos = all(s > 0 for s in low_scores)
    low_all_neg = all(s < 0 for s in low_scores)
    high_all_pos = all(s > 0 for s in high_scores)
    high_all_neg = all(s < 0 for s in high_scores)
    return (low_all_pos and high_all_pos) or (low_all_neg and high_all_neg)


# ============== 综合判定入口 ==============

def evaluate_measurement(
    value: float,
    target: float,
    sd: float,
    history: List[float],
) -> Dict[str, Any]:
    """
    判定单条数据的状态（单水平内）

    参数:
        value: 当前录入值
        target: 靶值
        sd: 标准差
        history: 该水平的历史值列表（按时间顺序，不含当前 value）

    返回:
        {
            "status": Status,
            "rules": List[str],  # 触发的规则列表
        }
    """
    rules: List[str] = []

    # 1. 警告规则 1_2s（独立标记）
    if check_1_2s(value, target, sd):
        rules.append("1_2s")

    # 2. 失控规则（按 1_3s → 2_2s → 4_1s → 10_x 顺序）
    # 当前值 + 历史 = 完整序列
    full = list(history) + [value]

    # 2.1 1_3s
    if check_1_3s(value, target, sd):
        rules.append("1_3s")

    # 2.2 2_2s（用 history + value 判定）
    if check_2_2s(full, target, sd):
        rules.append("2_2s")

    # 2.3 4_1s（用 full 判定）
    if check_4_1s(full, target, sd):
        rules.append("4_1s")

    # 2.4 10_x（用 full 判定）
    if check_10_x(full, target, sd):
        rules.append("10_x")

    # 状态
    out_of_control = any(r in OUT_OF_CONTROL_RULES for r in rules)
    if out_of_control:
        status = Status.OUT_OF_CONTROL
    elif rules:  # 有警告规则
        status = Status.WARNING
    else:
        status = Status.IN_CONTROL

    return {
        "status": status,
        "rules": rules,
    }


def evaluate_day(
    low_value: Optional[float], low_target: float, low_sd: float,
    high_value: Optional[float], high_target: float, high_sd: float,
    low_history: List[float], high_history: List[float],
) -> Dict[str, Any]:
    """
    同日合并扫描（跨水平规则 R_4s、跨水平 2_2s、跨水平 4_1s、跨水平 10_x）

    返回:
        {
            "low_status": Status,
            "low_rules": List[str],
            "high_status": Status,
            "high_rules": List[str],
        }
    """
    low_rules: List[str] = []
    high_rules: List[str] = []

    # === 低水平判定 ===
    if low_value is not None:
        # 1_2s
        if check_1_2s(low_value, low_target, low_sd):
            low_rules.append("1_2s")
        # 1_3s
        if check_1_3s(low_value, low_target, low_sd):
            low_rules.append("1_3s")
        # 2_2s
        full_low = list(low_history) + [low_value]
        if check_2_2s(full_low, low_target, low_sd):
            low_rules.append("2_2s")
        # 4_1s（同水平）
        if check_4_1s(full_low, low_target, low_sd):
            low_rules.append("4_1s")
        # 10_x（同水平）
        if check_10_x(full_low, low_target, low_sd):
            low_rules.append("10_x")

    # === 高水平判定 ===
    if high_value is not None:
        if check_1_2s(high_value, high_target, high_sd):
            high_rules.append("1_2s")
        if check_1_3s(high_value, high_target, high_sd):
            high_rules.append("1_3s")
        full_high = list(high_history) + [high_value]
        if check_2_2s(full_high, high_target, high_sd):
            high_rules.append("2_2s")
        if check_4_1s(full_high, high_target, high_sd):
            high_rules.append("4_1s")
        if check_10_x(full_high, high_target, high_sd):
            high_rules.append("10_x")

    # === 跨水平规则 ===
    # R_4s
    if check_r_4s(low_value, low_target, low_sd, high_value, high_target, high_sd):
        if low_value is not None and "R_4s" not in low_rules:
            low_rules.append("R_4s")
        if high_value is not None and "R_4s" not in high_rules:
            high_rules.append("R_4s")

    # 跨水平 4_1s
    if check_4_1s_cross_level(
        low_history + ([low_value] if low_value is not None else []),
        low_target, low_sd,
        high_history + ([high_value] if high_value is not None else []),
        high_target, high_sd,
    ):
        if low_value is not None and "4_1s" not in low_rules:
            low_rules.append("4_1s")
        if high_value is not None and "4_1s" not in high_rules:
            high_rules.append("4_1s")

    # 跨水平 10_x
    if check_10_x_cross_level(
        low_history + ([low_value] if low_value is not None else []),
        low_target, low_sd,
        high_history + ([high_value] if high_value is not None else []),
        high_target, high_sd,
    ):
        if low_value is not None and "10_x" not in low_rules:
            low_rules.append("10_x")
        if high_value is not None and "10_x" not in high_rules:
            high_rules.append("10_x")

    # 跨水平 2_2s：通过 check_2_2s 的跨水平参数判断
    # 低水平 → 传入高水平当日值
    if low_value is not None and high_value is not None:
        low_full = list(low_history) + [low_value]
        high_full = list(high_history) + [high_value]
        if check_2_2s(
            low_full, low_target, low_sd,
            cross_value=high_value, cross_target=high_target, cross_sd=high_sd,
        ):
            if "2_2s" not in low_rules:
                low_rules.append("2_2s")
        if check_2_2s(
            high_full, high_target, high_sd,
            cross_value=low_value, cross_target=low_target, cross_sd=low_sd,
        ):
            if "2_2s" not in high_rules:
                high_rules.append("2_2s")

    # === 状态 ===
    def _status(rules: List[str]) -> Status:
        if any(r in OUT_OF_CONTROL_RULES for r in rules):
            return Status.OUT_OF_CONTROL
        if rules:
            return Status.WARNING
        return Status.IN_CONTROL

    return {
        "low_status": _status(low_rules),
        "low_rules": low_rules,
        "high_status": _status(high_rules),
        "high_rules": high_rules,
    }
