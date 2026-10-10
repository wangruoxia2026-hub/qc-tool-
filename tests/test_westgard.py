"""
Westgard 规则自动判定 - 单元测试

按 spec §D.4 验收标准 a-i 编写测试。
"""
import pytest
from qc_tool.westgard import (
    sd_score,
    check_1_2s,
    check_1_3s,
    check_2_2s,
    check_r_4s,
    check_4_1s,
    check_10_x,
    evaluate_measurement,
    evaluate_day,
    Status,
    RULES,
)


# ============== 基础工具测试 ==============

class TestSdScore:
    """浮点 SD 分数计算（取整到 3 位小数后比较）"""

    def test_正数超出2sd(self):
        """value = target + 2.01*sd → sd_score ≈ 2.01"""
        score = sd_score(value=12.01, target=10.0, sd=1.0)
        assert score == pytest.approx(2.01, abs=1e-9)

    def test_负数超出2sd(self):
        score = sd_score(value=7.99, target=10.0, sd=1.0)
        assert score == pytest.approx(-2.01, abs=1e-9)

    def test_等于控制限(self):
        """value 正好 = target + 2*sd → sd_score = 2.0（浮点等于不算超出）"""
        score = sd_score(value=12.0, target=10.0, sd=1.0)
        assert score == 2.0

    def test_浮点取整到3位(self):
        """3.0000000001 视为 3.000，不算超出"""
        score = sd_score(value=13.0000001, target=10.0, sd=1.0)
        # 取整到 3 位小数后是 3.000
        assert round(score, 3) == 3.0


# ============== 警告规则 1_2s ==============

class TestCheck1_2s:
    """验收 a: |value| = 2.01SD → 警告(1_2s)"""

    def test_刚好2sd不算超出(self):
        """|value| = 2.000SD → 不触发（浮点等于不算超出）"""
        assert check_1_2s(value=12.0, target=10.0, sd=1.0) is False

    def test_201sd触发警告(self):
        """|value| = 2.01SD → 触发"""
        assert check_1_2s(value=12.01, target=10.0, sd=1.0) is True

    def test_负方向201sd触发警告(self):
        assert check_1_2s(value=7.99, target=10.0, sd=1.0) is True

    def test_正常值不触发(self):
        assert check_1_2s(value=10.5, target=10.0, sd=1.0) is False

    def test_15sd不触发(self):
        assert check_1_2s(value=11.5, target=10.0, sd=1.0) is False


# ============== 失控规则 1_3s ==============

class TestCheck1_3s:
    """验收 b: |value| = 3.01SD → 失控(1_3s)"""

    def test_刚好3sd不算超出(self):
        """|value| = 3.000SD → 不触发（浮点等于不算超出）"""
        assert check_1_3s(value=13.0, target=10.0, sd=1.0) is False

    def test_301sd触发失控(self):
        """|value| = 3.01SD → 触发"""
        assert check_1_3s(value=13.01, target=10.0, sd=1.0) is True

    def test_负方向301sd触发失控(self):
        assert check_1_3s(value=6.99, target=10.0, sd=1.0) is True

    def test_201sd不触发13s(self):
        assert check_1_3s(value=12.01, target=10.0, sd=1.0) is False


# ============== 失控规则 2_2s ==============

class TestCheck2_2s:
    """验收 c: 同水平连续 2 条 |value| > 2SD 同方向 → 后一条失控
    验收 d: 跨水平同日都 |value| > 2SD 同方向 → 当日两条均失控"""

    def test_同水平连续2条正方向触发(self):
        """同一水平连续 2 个正方向 > 2SD → 第二条触发"""
        history = [12.01, 12.02]  # 都是 +2.0X SD
        assert check_2_2s(history, target=10.0, sd=1.0) is True

    def test_同水平连续2条负方向触发(self):
        history = [7.99, 7.98]  # 都是 -2.0X SD
        assert check_2_2s(history, target=10.0, sd=1.0) is True

    def test_同水平2条一正一负不触发(self):
        history = [12.01, 7.99]
        assert check_2_2s(history, target=10.0, sd=1.0) is False

    def test_同水平只有1条不触发(self):
        """历史数据不足，仅 1 条时不触发"""
        history = [12.01]
        assert check_2_2s(history, target=10.0, sd=1.0) is False

    def test_同水平第二条刚好2sd不触发(self):
        """第二条 = 2.000 SD，浮点等于不算超出 → 不触发"""
        history = [12.01, 12.00]
        assert check_2_2s(history, target=10.0, sd=1.0) is False

    def test_跨水平同日两水平各1条同方向触发(self):
        """跨水平同日：低水平 +2SD + 高水平 +2SD → 两条都触发
        判定时传入 [L_value] + cross_value=H_value（或反向）"""
        # L 水平 history=[12.01]，传入 H 当日值 22.01 → 跨水平 2_2s 触发
        triggered = check_2_2s(
            [12.01], target=10.0, sd=1.0,
            cross_value=22.01, cross_target=20.0, cross_sd=1.0,
        )
        assert triggered is True

    def test_跨水平同日两水平各1条反方向不触发(self):
        """L +2SD, H -2SD → 反方向 → 跨水平 2_2s 不触发"""
        triggered = check_2_2s(
            [12.01], target=10.0, sd=1.0,
            cross_value=17.99, cross_target=20.0, cross_sd=1.0,
        )
        assert triggered is False

    def test_跨水平同日另一水平没超2sd不触发(self):
        """L +2SD, H 1.5SD → 另一水平不超 2SD → 不触发"""
        triggered = check_2_2s(
            [12.01], target=10.0, sd=1.0,
            cross_value=21.5, cross_target=20.0, cross_sd=1.0,
        )
        assert triggered is False

    def test_跨水平无cross参数不触发(self):
        """不传跨水平参数 → 仅同水平连续 2 条判定（1 条不触发）"""
        assert check_2_2s([12.01], target=10.0, sd=1.0) is False


# ============== 失控规则 R_4s ==============

class TestCheckR4s:
    """验收 e: 同一测定日期一个 +2SD 另一个 −2SD → 两条均失控(R_4s)"""

    def test_跨水平一正一负极差4sd触发(self):
        """L = +2.01SD, H = -2.01SD → 极差 = 4.02SD ≥ 4SD"""
        triggered = check_r_4s(
            low_value=12.01, low_target=10.0, low_sd=1.0,
            high_value=17.99, high_target=20.0, high_sd=1.0,
        )
        assert triggered is True

    def test_跨水平同方向不触发(self):
        """L 和 H 同方向 → 不触发 R_4s"""
        triggered = check_r_4s(
            low_value=12.01, low_target=10.0, low_sd=1.0,
            high_value=22.01, high_target=20.0, high_sd=1.0,
        )
        assert triggered is False

    def test_跨水平极差刚好4sd触发(self):
        """L = +2SD, H = -2SD → 极差 = 4SD ≥ 4SD（取整后边界）"""
        triggered = check_r_4s(
            low_value=12.0, low_target=10.0, low_sd=1.0,
            high_value=18.0, high_target=20.0, high_sd=1.0,
        )
        # spec D.5: 浮点等于判断取整到 3 位小数后比较
        # 4.0 == 4.0 → 触发（≥ 4SD 算失控）
        assert triggered is True

    def test_跨水平极差小于4sd不触发(self):
        """L = +1SD, H = -1SD → 极差 = 2SD < 4SD"""
        triggered = check_r_4s(
            low_value=11.0, low_target=10.0, low_sd=1.0,
            high_value=19.0, high_target=20.0, high_sd=1.0,
        )
        assert triggered is False

    def test_只有一个水平数据不触发(self):
        """缺另一个水平数据 → 不触发"""
        triggered = check_r_4s(
            low_value=12.01, low_target=10.0, low_sd=1.0,
            high_value=None, high_target=20.0, high_sd=1.0,
        )
        assert triggered is False


# ============== 失控规则 4_1s ==============

class TestCheck4_1s:
    """验收 f: 同水平连续 4 个 |value| > 1SD 同方向 → 第 4 条失控
    验收 g: 跨水平两水平各连续 2 次同方向 |value| > 1SD → 第 4 条失控
    验收 i: 同水平连续 4 个超 ±1SD 但不超 ±2SD → 触发 4_1s，不触发 1_2s"""

    def test_同水平连续4个正方向触发(self):
        """+1.1, +1.2, +1.3, +1.4 → 第 4 条触发"""
        history = [11.1, 11.2, 11.3, 11.4]
        assert check_4_1s(history, target=10.0, sd=1.0) is True

    def test_同水平连续4个负方向触发(self):
        history = [8.9, 8.8, 8.7, 8.6]
        assert check_4_1s(history, target=10.0, sd=1.0) is True

    def test_同水平只有3个不触发(self):
        history = [11.1, 11.2, 11.3]
        assert check_4_1s(history, target=10.0, sd=1.0) is False

    def test_同水平第3个反向不触发(self):
        """+1.1, +1.2, -1.3, +1.4 → 不触发（第 3 个反向打断连续）"""
        history = [11.1, 11.2, 8.7, 11.4]
        assert check_4_1s(history, target=10.0, sd=1.0) is False

    def test_同水平第4个刚好1sd不触发(self):
        """第 4 个 = 1.000 SD → 浮点等于不算超出 → 不触发"""
        history = [11.1, 11.2, 11.3, 11.0]
        assert check_4_1s(history, target=10.0, sd=1.0) is False

    def test_同水平第4个超1sd触发(self):
        """第 4 个 = 1.01 SD → 触发"""
        history = [11.1, 11.2, 11.3, 11.01]
        assert check_4_1s(history, target=10.0, sd=1.0) is True

    def test_跨水平两水平各2次同方向触发(self):
        """L 水平 +1.1, +1.2；H 水平 +1.1, +1.2 → 第 4 条触发
        跨水平情况下，L 和 H 各自的判定序列分别累积"""
        # L 水平累积
        l_history = [11.1, 11.2]
        # H 水平累积
        h_history = [21.1, 21.2]
        # 跨水平判定：两个水平都需触发
        assert check_4_1s(l_history, target=10.0, sd=1.0) is False  # L 单独不够
        assert check_4_1s(h_history, target=20.0, sd=1.0) is False  # H 单独不够
        # 跨水平函数：两个水平各累积 ≥ 2 条同方向 → 触发
        triggered = check_4_1s_cross_level(
            low_history=[11.1, 11.2], low_target=10.0, low_sd=1.0,
            high_history=[21.1, 21.2], high_target=20.0, high_sd=1.0,
        )
        assert triggered is True

    def test_跨水平只有一个水平2次不触发(self):
        triggered = check_4_1s_cross_level(
            low_history=[11.1, 11.2], low_target=10.0, low_sd=1.0,
            high_history=[21.0], high_target=20.0, high_sd=1.0,
        )
        assert triggered is False

    def test_同水平4个超1sd不超2sd不触发12s(self):
        """验收 i: +1.1, +1.2, +1.3, +1.4 → 触发 4_1s，不触发 1_2s"""
        history = [11.1, 11.2, 11.3, 11.4]
        # 第 4 条 1_4 SD = 1.4 → 1_2s 不触发（≤ 2SD）
        for v in history:
            assert check_1_2s(v, target=10.0, sd=1.0) is False
        # 4_1s 触发
        assert check_4_1s(history, target=10.0, sd=1.0) is True


# ============== 失控规则 10_x ==============

class TestCheck10X:
    """验收 h: 累计 10 个同侧值 → 第 10 条失控(10_x)"""

    def test_10个正方向触发(self):
        history = [10.5] * 10  # 都是 +0.5 SD
        assert check_10_x(history, target=10.0, sd=1.0) is True

    def test_10个负方向触发(self):
        history = [9.5] * 10  # 都是 -0.5 SD
        assert check_10_x(history, target=10.0, sd=1.0) is True

    def test_9个不触发(self):
        history = [10.5] * 9
        assert check_10_x(history, target=10.0, sd=1.0) is False

    def test_10个中混1个反向不触发(self):
        """9 个正方向 + 1 个负方向 → 不算同侧连续 10 个"""
        history = [10.5] * 9 + [9.5]
        assert check_10_x(history, target=10.0, sd=1.0) is False

    def test_跨水平两水平各5次同方向触发(self):
        """L 水平 +0.5 × 5；H 水平 +0.5 × 5 → 第 10 条触发
        跨水平累积"""
        l_history = [10.5] * 5
        h_history = [20.5] * 5
        # 各自单独 5 个不够
        assert check_10_x(l_history, target=10.0, sd=1.0) is False
        assert check_10_x(h_history, target=20.0, sd=1.0) is False
        # 跨水平合并后 = 10 个同方向 → 触发
        triggered = check_10_x_cross_level(
            low_history=l_history, low_target=10.0, low_sd=1.0,
            high_history=h_history, high_target=20.0, high_sd=1.0,
        )
        assert triggered is True


# ============== 综合判定 ==============

class TestEvaluateMeasurement:
    """综合判定入口：单条数据状态"""

    def test_验收a_201sd警告(self):
        """|value| = 2.01SD → 警告(1_2s) 黄色"""
        # value = 12.01, target=10, sd=1 → +2.01SD
        result = evaluate_measurement(
            value=12.01, target=10.0, sd=1.0,
            history=[],  # 没有历史数据
        )
        assert result["status"] == Status.WARNING
        assert "1_2s" in result["rules"]
        assert "1_3s" not in result["rules"]

    def test_验收b_301sd失控13s(self):
        """|value| = 3.01SD → 失控(1_3s) 红色"""
        result = evaluate_measurement(
            value=13.01, target=10.0, sd=1.0,
            history=[],
        )
        assert result["status"] == Status.OUT_OF_CONTROL
        assert "1_3s" in result["rules"]
        # 1_3s 必然伴随 1_2s（spec D.2 第 5 点）
        assert "1_2s" in result["rules"]

    def test_在控(self):
        result = evaluate_measurement(
            value=10.5, target=10.0, sd=1.0,
            history=[],
        )
        assert result["status"] == Status.IN_CONTROL
        assert result["rules"] == []

    def test_刚好2sd在控(self):
        """|value| = 2.000SD → 在控（浮点等于不算超出）"""
        result = evaluate_measurement(
            value=12.0, target=10.0, sd=1.0,
            history=[],
        )
        assert result["status"] == Status.IN_CONTROL

    def test_验收c_同水平连续2条_2s(self):
        """|value| > 2SD 同方向连续 2 条 → 后一条失控(2_2s)"""
        # 先录第一条 12.01（警告）
        first = evaluate_measurement(
            value=12.01, target=10.0, sd=1.0,
            history=[],
        )
        assert first["status"] == Status.WARNING

        # 第二条 12.02（同方向）→ 触发 2_2s
        second = evaluate_measurement(
            value=12.02, target=10.0, sd=1.0,
            history=[12.01],
        )
        assert second["status"] == Status.OUT_OF_CONTROL
        assert "2_2s" in second["rules"]

    def test_验收h_10个同侧(self):
        """累计 10 个同侧值 → 第 10 条失控(10_x)"""
        # 录入前 9 个 +0.5 SD
        for i in range(9):
            result = evaluate_measurement(
                value=10.5, target=10.0, sd=1.0,
                history=[10.5] * i,
            )
            assert result["status"] == Status.IN_CONTROL

        # 第 10 个
        result = evaluate_measurement(
            value=10.5, target=10.0, sd=1.0,
            history=[10.5] * 9,
        )
        assert result["status"] == Status.OUT_OF_CONTROL
        assert "10_x" in result["rules"]

    def test_验收i_4_1s不伴随1_2s(self):
        """4 个超 1SD 但不超 2SD → 第 4 条触发 4_1s，不触发 1_2s"""
        # 前 3 个 +1.x SD（不超 2SD）
        for i, v in enumerate([11.1, 11.2, 11.3]):
            result = evaluate_measurement(
                value=v, target=10.0, sd=1.0,
                history=[11.1, 11.2, 11.3][:i],
            )
            assert result["status"] == Status.IN_CONTROL
            assert "1_2s" not in result["rules"]

        # 第 4 个
        result = evaluate_measurement(
            value=11.4, target=10.0, sd=1.0,
            history=[11.1, 11.2, 11.3],
        )
        assert result["status"] == Status.OUT_OF_CONTROL
        assert "4_1s" in result["rules"]
        assert "1_2s" not in result["rules"]


class TestEvaluateDay:
    """同日合并扫描：跨水平规则（验收 d、e、g）"""

    def test_验收d_跨水平同日同方向_2_2s(self):
        """跨水平同日 L 和 H 都 |value| > 2SD 同方向 → 两条均失控"""
        result = evaluate_day(
            low_value=12.01, low_target=10.0, low_sd=1.0,
            high_value=22.01, high_target=20.0, high_sd=1.0,
            low_history=[],
            high_history=[],
        )
        assert result["low_status"] == Status.OUT_OF_CONTROL
        assert "2_2s" in result["low_rules"]
        assert result["high_status"] == Status.OUT_OF_CONTROL
        assert "2_2s" in result["high_rules"]

    def test_验收e_跨水平一正一负_R_4s(self):
        """L = +2.01SD, H = -2.01SD → 两条均失控(R_4s)"""
        result = evaluate_day(
            low_value=12.01, low_target=10.0, low_sd=1.0,
            high_value=17.99, high_target=20.0, high_sd=1.0,
            low_history=[],
            high_history=[],
        )
        assert result["low_status"] == Status.OUT_OF_CONTROL
        assert "R_4s" in result["low_rules"]
        assert result["high_status"] == Status.OUT_OF_CONTROL
        assert "R_4s" in result["high_rules"]


# ============== 批号切换 ==============

class TestLotSwitch:
    """边界：切换批号时规则历史清零"""

    def test_批号切换历史清零(self):
        """批号 A 下累积 1 条 12.01（+2.01SD），切到批号 B 录新值 → 视为无历史"""
        # 批号 A：前 1 条 12.01，再录 12.02 触发 2_2s
        a_result = evaluate_measurement(
            value=12.02, target=10.0, sd=1.0,
            history=[12.01],
        )
        assert "2_2s" in a_result["rules"]

        # 批号 B：录 12.02 但历史为空（不继承批号 A 的历史）→ 仅警告
        b_result = evaluate_measurement(
            value=12.02, target=10.0, sd=1.0,
            history=[],  # 批号 B 的 history 应该是空
        )
        assert b_result["status"] == Status.WARNING
        assert "2_2s" not in b_result["rules"]


# ============== 多规则同时触发 ==============

class TestMultipleRules:
    """边界：同时触发多条规则 → 状态列全部列出"""

    def test_1_3s伴随1_2s(self):
        """|value| = 3.01SD 同时触发 1_3s 和 1_2s"""
        result = evaluate_measurement(
            value=13.01, target=10.0, sd=1.0,
            history=[],
        )
        assert "1_3s" in result["rules"]
        assert "1_2s" in result["rules"]

    def test_2_2s伴随1_2s(self):
        """|value| = 2.01SD 连续 2 条 → 触发 2_2s 同时每条都触发 1_2s"""
        # 第一条 12.01
        first = evaluate_measurement(
            value=12.01, target=10.0, sd=1.0,
            history=[],
        )
        assert "1_2s" in first["rules"]

        # 第二条 12.02 触发 2_2s
        second = evaluate_measurement(
            value=12.02, target=10.0, sd=1.0,
            history=[12.01],
        )
        assert "2_2s" in second["rules"]
        assert "1_2s" in second["rules"]


# ============== 辅助函数（跨水平）==============

def check_4_1s_cross_level(
    low_history, low_target, low_sd,
    high_history, high_target, high_sd,
):
    """跨水平 4_1s：两水平各连续 2 次同方向超 1SD → 触发"""
    from qc_tool.westgard import check_4_1s
    # 低水平累积到 ≥ 2 条
    low_ok = len(low_history) >= 2 and check_4_1s(low_history, low_target, low_sd) or False
    # 但这里 check_4_1s 需要 4 条才触发
    # 跨水平特殊：两水平各 ≥ 2 条且同方向 → 触发
    # 提取低水平最近 2 条方向
    if len(low_history) < 2 or len(high_history) < 2:
        return False
    low_dirs = [1 if v > low_target else -1 for v in low_history[-2:]]
    high_dirs = [1 if v > high_target else -1 for v in high_history[-2:]]
    if not all(d > 0 for d in low_dirs) and not all(d < 0 for d in low_dirs):
        return False
    if not all(d > 0 for d in high_dirs) and not all(d < 0 for d in high_dirs):
        return False
    return all(d > 0 for d in low_dirs) == all(d > 0 for d in high_dirs)


def check_10_x_cross_level(
    low_history, low_target, low_sd,
    high_history, high_target, high_sd,
):
    """跨水平 10_x：两水平各连续 ≥ 5 次同方向 → 触发"""
    if len(low_history) < 5 or len(high_history) < 5:
        return False
    low_recent = low_history[-5:]
    high_recent = high_history[-5:]
    low_all_pos = all(v > low_target for v in low_recent)
    low_all_neg = all(v < low_target for v in low_recent)
    high_all_pos = all(v > high_target for v in high_recent)
    high_all_neg = all(v < high_target for v in high_recent)
    return (low_all_pos and high_all_pos) or (low_all_neg and high_all_neg)
