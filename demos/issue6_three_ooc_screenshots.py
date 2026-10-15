"""
issue #6（spec §F.1 失控排查方向提示面板）— 三场景面板快照演示（无 GUI）

目的：
  跑 3 种失控（1_3s / 4_1s / 1_3s+2_2s），用 build_investigation_plan 返回
  的 categories 渲染成纯文本面板，写到 demos/issue6_*.txt，等价"截图"
  的可读版；CI 可直接跑。
不入 git：跑完删。

跑法：
  python demos/issue6_three_ooc_screenshots.py
  输出文件：
    demos/issue6_1_3s.txt
    demos/issue6_4_1s.txt
    demos/issue6_1_3s_2_2s.txt
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable

# 强制 UTF-8 输出（避免 cmd 中文乱码）
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.stdout.reconfigure(encoding="utf-8")

# 把项目根加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool.investigation import build_investigation_plan


# ---------------------------------------------------------------------------
# 触发场景：纯 mock，不真用 Westgard，只构造 plan 需要的 rules
# ---------------------------------------------------------------------------

SCENARIOS: list[dict] = [
    {
        "name": "issue6_1_3s",
        "title": "场景 1 —— 单点 1_3s 失控（spec §F.1 b）",
        "rules": ["1_3s"],
        "triggered_date": "2026-04-05",
        "project_name": "维生素 D",
        "test_item_name": "D2",
        "lot": "LOT1",
        "level": "L",
        "value": 107.000,
    },
    {
        "name": "issue6_4_1s",
        "title": "场景 2 —— 连续 4 点超 +1SD 触发 4_1s（spec §F.1 b）",
        "rules": ["4_1s"],
        "triggered_date": "2026-04-08",
        "project_name": "维生素 D",
        "test_item_name": "D2",
        "lot": "LOT1",
        "level": "L",
        "value": 102.500,
    },
    {
        "name": "issue6_1_3s_2_2s",
        "title": "场景 3 —— 1_3s + 2_2s 同时触发（spec §F.1 e：合并、不重复不遗漏）",
        "rules": ["1_3s", "2_2s"],
        "triggered_date": "2026-04-12",
        "project_name": "维生素 D",
        "test_item_name": "D2",
        "lot": "LOT1",
        "level": "L",
        "value": 105.200,
    },
]


# 罗马数字（仅供面板显示好看，无业务含义）
_ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]


def render_panel(scenario: dict) -> str:
    """把 build_investigation_plan 返回值渲染成面板文本。"""
    plan = build_investigation_plan(
        rules=scenario["rules"],
        triggered_date=scenario["triggered_date"],
        project_name=scenario["project_name"],
        test_item_name=scenario["test_item_name"],
        lot=scenario["lot"],
        level=scenario["level"],
        value=scenario["value"],
        checklist={},  # 初始未勾选
    )

    ctx = plan["context"]
    cats = plan["categories"]

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"  失控排查方向提示面板（spec V1.3 §F.1）")
    lines.append("=" * 72)
    lines.append(f"  触发日期     ：{ctx['triggered_date']}")
    lines.append(f"  检验项目     ：{ctx['project_name']}")
    lines.append(f"  检测指标     ：{ctx['test_item_name']}")
    lines.append(f"  批号 / 水平  ：{ctx['lot']} / {ctx['level']}")
    lines.append(f"  测量值       ：{ctx['value']:.3f}")
    lines.append(f"  触发规则     ：{', '.join(ctx['rules'])}")
    lines.append("-" * 72)
    lines.append(f"  共 {len(cats)} 个排查方向分类（按 spec §F.1 合并去重）")
    lines.append("=" * 72)
    lines.append("")

    for cat in cats:
        lines.append(f"【{cat['category']}】（{len(cat['items'])} 项）")
        lines.append("-" * 72)
        for idx, item in enumerate(cat["items"]):
            roman = _ROMAN[idx] if idx < len(_ROMAN) else str(idx + 1)
            lines.append(f"  [ ] {roman:<5} {item}")
        lines.append("")

    lines.append("=" * 72)
    lines.append("  处理结论：[ 可填可不填（spec §F.1 d） ]")
    lines.append("=" * 72)
    lines.append("")
    lines.append("  操作按钮：[ 保存为失控记录 ]  [ 取消 ]")
    lines.append("  必填校验：已检查项一项不勾不许保存（spec §F.1 d）")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自检断言（CI 可验）
# ---------------------------------------------------------------------------

def assert_panel(panel: str, scenario: dict) -> list[str]:
    """对渲染结果做硬性断言；返回错误信息列表（空 = 全部通过）。"""
    errs: list[str] = []
    rules = scenario["rules"]

    # 1) 标题与触发规则
    if "失控排查方向提示面板" not in panel:
        errs.append("缺标题")
    if f"触发规则     ：{', '.join(rules)}" not in panel:
        errs.append(f"触发规则显示错：{rules}")

    # 2) 1_3s/R_4s → 仅"随机误差嫌疑"；4_1s/10_x → 仅"系统误差嫌疑"；
    #    含 2_2s → 两类都有
    only_random = set(rules).issubset({"1_3s", "R_4s"})
    only_system = set(rules).issubset({"4_1s", "10_x"})
    has_random = "随机误差嫌疑" in panel
    has_system = "系统误差嫌疑" in panel

    if only_random and not has_random:
        errs.append("应展示'随机误差嫌疑'分类")
    if only_system and not has_system:
        errs.append("应展示'系统误差嫌疑'分类")
    if "2_2s" in rules:
        if not (has_random and has_system):
            errs.append("含 2_2s 必须同时展示两类")

    # 3) 逐字文案（与 spec §F.1 一致；任一缺失即失败）
    random_lines = [
        "试剂：是否新开瓶 / 是否过期 / 是否污染",
        "仪器：泵阀、管道气泡、光路、电压",
        "操作：加样量、混匀、读数时机",
        "质控品：复溶是否充分 / 是否污染",
        "加样/混匀/读数等单次操作失误",
    ]
    system_lines = [
        "校准：是否近期校准 / 标准曲线是否漂移",
        "试剂：批号更换 / 厂家变更",
        "质控品：靶值与当前批是否一致",
        "环境：温度、湿度",
        "方法学偏倚：自身 2s 范围只代表随机误差水平，需结合方法学偏倚判断",
        "耗材：批号更换 / 厂家变更",
    ]
    if has_random:
        for line in random_lines:
            if line not in panel:
                errs.append(f"随机清单缺：{line!r}")
    if has_system:
        for line in system_lines:
            if line not in panel:
                errs.append(f"系统清单缺：{line!r}")

    # 4) 合并不重复不遗漏：去重后 5+6=11 项
    if "2_2s" in rules and "1_3s" in rules:
        # 随机 5 + 系统 6 = 11 行 "[ ]" 勾选
        checkbox_count = panel.count("[ ]")
        if checkbox_count < 11:
            errs.append(f"合并后勾选项数 < 11（实际 {checkbox_count}）")

    # 5) UI 元素提示
    if "处理结论" not in panel:
        errs.append("缺'处理结论'输入区")
    if "已检查项一项不勾不许保存" not in panel:
        errs.append("缺必填校验提示")
    if "保存为失控记录" not in panel:
        errs.append("缺'保存为失控记录'按钮")

    return errs


def main() -> int:
    out_dir = ROOT / "demos"
    out_dir.mkdir(exist_ok=True)

    print(f"输出目录：{out_dir}")
    print()

    fail = 0
    for sc in SCENARIOS:
        print(f"▶ {sc['title']}")
        panel = render_panel(sc)

        out_path = out_dir / f"{sc['name']}.txt"
        out_path.write_text(panel, encoding="utf-8")
        print(f"  写入：{out_path}")

        errs = assert_panel(panel, sc)
        if errs:
            fail += 1
            for e in errs:
                print(f"  ❌ {e}")
        else:
            n_cat = panel.count("【")
            print(f"  ✅ 断言通过（{n_cat} 个分类）")

        # 把面板内容也打印到控制台（每文件前 16 行）
        print()
        for line in panel.splitlines()[:16]:
            print(f"    {line}")
        print(f"    ...（共 {len(panel.splitlines())} 行，详见 {out_path.name}）")
        print()

    print("=" * 60)
    if fail == 0:
        print(f"  ✅ 全部 {len(SCENARIOS)} 个场景通过断言")
        return 0
    print(f"  ❌ {fail} / {len(SCENARIOS)} 场景失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
