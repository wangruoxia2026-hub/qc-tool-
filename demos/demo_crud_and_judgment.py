"""
d1 演示：CRUD 中文 + Westgard 6 条规则 Levey-Jennings 图
不入 git，跑完删。
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
from pathlib import Path

# 强制 UTF-8 输出（避免 cmd 中文乱码）
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.stdout.reconfigure(encoding="utf-8")

# 把项目根加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qc_tool import projects as P
from qc_tool import westgard as W
from qc_tool.db import init_database


# ============================================================
# 第一部分：CRUD 中文演示
# ============================================================

def banner(text: str) -> None:
    print()
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)


def show(label: str, ok: bool, detail: str = "") -> None:
    icon = "✅" if ok else "❌"
    print(f"  {icon} {label}" + (f"  →  {detail}" if detail else ""))


def crud_demo() -> None:
    banner("功能 A 业务层（spec §A）CRUD 中文演示")

    # 建临时库
    tmp = Path(tempfile.mkdtemp()) / "demo.db"
    init_database(tmp)
    # 用 get_connection 拿新连接（确保 PRAGMA foreign_keys=ON，否则 CASCADE 不生效）
    from qc_tool.db import get_connection
    conn = get_connection(tmp)
    conn.row_factory = sqlite3.Row

    # --- 1. 建项目"维生素 D" ---
    print('\n[1] 新建检验项目"维生素 D"')
    p = P.create_project(conn, name="维生素 D", short="Vit-D",
                         unit="ng/mL", manufacturer="Roche")
    show("create_project 成功", True, f"id={p.id}, name={p.name!r}, short={p.short!r}")

    # --- 2. 列出项目 ---
    print("\n[2] 列出所有项目")
    for proj in P.list_projects(conn):
        show("list_projects", True, f"id={proj.id}  {proj.name}  (简称={proj.short})")

    # --- 3. 重名再建 ---
    print('\n[3] 再次创建同名项目"维生素 D"（应拒绝）')
    try:
        P.create_project(conn, name="维生素 D")
        show("拒绝重名", False, "不应该成功！")
    except P.ProjectNameExists as e:
        show("拒绝重名", True, f"抛 {type(e).__name__}: {e}")

    # --- 4. 项目名空 ---
    print("\n[4] 项目名留空（应拒绝）")
    try:
        P.create_project(conn, name="")
        show("拒绝空名", False, "不应该成功！")
    except P.ProjectNameEmpty as e:
        show("拒绝空名", True, f"抛 {type(e).__name__}: {e}")

    # --- 5. 建指标"维生素 D2"，批号 L001, L 水平, 靶值 100, SD 2 ---
    print('\n[5] 新建检测指标"维生素 D2"（批号 L001, 水平 L, 靶值 100, SD 2）')
    ti = P.create_test_item(conn, p.id, name="维生素 D2", lot="L001",
                            level="L", target=100.0, sd=2.0)
    show("create_test_item 成功", True,
         f"id={ti.id}, name={ti.name!r}, lot={ti.lot!r}, level={ti.level!r}, "
         f"target={ti.target}, sd={ti.sd}")

    # --- 6. 列出指标 ---
    print("\n[6] 列出该项目下所有检测指标")
    for t in P.list_test_items(conn, p.id):
        show("list_test_items", True,
             f"id={t.id}  {t.name}  批号={t.lot}  水平={t.level}  "
             f"靶值={t.target}  SD={t.sd}")

    # --- 7. 靶值 0 ---
    print("\n[7] 靶值填 0（应拒绝）")
    try:
        P.create_test_item(conn, p.id, name="D2-bad", lot="L002",
                          level="L", target=0, sd=2.0)
        show("拒绝靶值 0", False, "不应该成功！")
    except P.TargetMustPositive as e:
        show("拒绝靶值 0", True, f"抛 {type(e).__name__}: {e}")

    # --- 8. 靶值"abc" ---
    print("\n[8] 靶值填 'abc'（应拒绝）")
    try:
        P.create_test_item(conn, p.id, name="D2-bad", lot="L002",
                          level="L", target="abc", sd=2.0)
        show("拒绝非数字", False, "不应该成功！")
    except P.TargetInvalidNumber as e:
        show("拒绝非数字", True, f"抛 {type(e).__name__}: {e}")

    # --- 9. SD 0 ---
    print("\n[9] SD 填 0（应拒绝）")
    try:
        P.create_test_item(conn, p.id, name="D2-bad", lot="L002",
                          level="L", target=100, sd=0)
        show("拒绝 SD 0", False, "不应该成功！")
    except P.SDMustPositive as e:
        show("拒绝 SD 0", True, f"抛 {type(e).__name__}: {e}")

    # --- 10. 改靶值 ---
    print("\n[10] 改靶值 100 → 102（应成功）")
    new_ti = P.update_test_item(conn, ti.id, target=102.0)
    show("update_test_item 改靶值", new_ti.target == 102.0,
         f"靶值={new_ti.target}")

    # 还原靶值
    P.update_test_item(conn, ti.id, target=100.0)

    # --- 11. 实验室名称 ---
    print("\n[11] 首次新建项目时强制录入实验室名称（这里跳过，新建后再设）")
    # is_first_project_pending 现在应该 True（lab_name 仍空），
    # 跟"前面有项目"无关 — 只看 lab_name 是否已设
    pending = P.is_first_project_pending(conn)
    show("is_first_project_pending", pending is True,
         f"因为 lab_name 还没设 — pending={pending}")
    P.set_lab_name(conn, "检验科")
    show("set_lab_name 成功", P.get_lab_name(conn) == "检验科",
         f"lab_name={P.get_lab_name(conn)!r}")
    show("设置后 pending 变 False", P.is_first_project_pending(conn) is False,
         f"pending={P.is_first_project_pending(conn)}")

    # --- 12. 删指标（有未导出 ooc 应拒绝）---
    print("\n[12] 删指标（关联一个未导出 ooc → 应拒绝）")
    conn.execute(
        "INSERT INTO out_of_control("
        "  test_item_id, project_id, lot, level, triggered_date,"
        "  test_item_name, project_name, rules, conclusion"
        ") VALUES (?, 1, 'L001', 'L', '2026-10-11', '维生素 D2', '维生素 D',"
        "  '[\"1_3s\"]', '复测合格')",
        (ti.id,),
    )
    conn.commit()
    try:
        P.delete_test_item(conn, ti.id)
        show("拒绝删（有未导出 ooc）", False, "不应该成功！")
    except P.HasUnexportedOOC as e:
        show("拒绝删（有未导出 ooc）", True,
             f"抛 {type(e).__name__}: {e}")

    # --- 13. 让 ooc 关联到 measurement（=已脱钩 =已导出）---
    print("\n[13] 让 ooc 关联到真实 measurement（=已脱钩 → 可删）")
    conn.execute(
        "INSERT INTO measurements(test_item_id, project_id, lot, level, date, value) "
        "VALUES (?, 1, 'L001', 'L', '2026-10-11', 105.5)",
        (ti.id,),
    )
    mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE out_of_control SET measurement_id = ? WHERE test_item_id = ?",
        (mid, ti.id),
    )
    conn.commit()
    summary = P.delete_test_item(conn, ti.id)
    show("delete_test_item 成功", True,
         f"n_measurements={summary.n_measurements}")

    # 验证级联
    n_ti = conn.execute("SELECT COUNT(*) FROM test_items").fetchone()[0]
    n_meas = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    n_ooc = conn.execute("SELECT COUNT(*) FROM out_of_control").fetchone()[0]
    ooc_ti = conn.execute(
        "SELECT test_item_id, measurement_id, test_item_name, lot, level "
        "FROM out_of_control"
    ).fetchone()
    show("test_items 应为 0", n_ti == 0, f"实际 {n_ti}")
    show("measurements CASCADE 应为 0", n_meas == 0, f"实际 {n_meas}")
    show("out_of_control 应保留 1 条", n_ooc == 1, f"实际 {n_ooc}")
    show("ooc.test_item_id 置空", ooc_ti["test_item_id"] is None,
         f"实际 {ooc_ti['test_item_id']!r}")
    show("ooc.measurement_id 置空（FK SET NULL）",
         ooc_ti["measurement_id"] is None,
         f"实际 {ooc_ti['measurement_id']!r}")
    show("ooc.test_item_name 保留（前端兜底）",
         ooc_ti["test_item_name"] == "维生素 D2",
         f"实际 {ooc_ti['test_item_name']!r}")

    conn.close()
    print(f"\n  临时 db: {tmp}  （demo 结束自动清理）")


# ============================================================
# 第二部分：Westgard 6 条规则 Levey-Jennings 图
# ============================================================

def draw_lj_chart(values, target, sd, save_path: Path) -> None:
    """画一张 Levey-Jennings 控制图，点颜色按 Westgard 状态着色。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 准备：按顺序逐条判定
    n = len(values)
    history: list[float] = []
    points = []  # (idx, value, status, rules)
    for i, v in enumerate(values):
        res = W.evaluate_measurement(v, target, sd, history)
        points.append((i + 1, v, res["status"], res["rules"]))
        history.append(v)

    # 颜色 / 形状
    style_map = {
        W.Status.IN_CONTROL:  ("o", "black",  "在控"),
        W.Status.WARNING:     ("^", "#f4c430", "警告(1_2s)"),
        W.Status.OUT_OF_CONTROL: ("o", "red",  "失控"),
    }

    fig, ax = plt.subplots(figsize=(14, 7))
    # 7 条线
    for k, c, ls in [
        (-3, "red",  "-"),
        (-2, "#f4c430", "--"),
        (-1, "green", "--"),
        ( 0, "green",  "-"),
        ( 1, "green", "--"),
        ( 2, "#f4c430", "--"),
        ( 3, "red",  "-"),
    ]:
        ax.axhline(target + k * sd, color=c, linestyle=ls, linewidth=1.2,
                   alpha=0.7)
    ax.axhline(target, color="green", linewidth=1.8,
               label=f"中心线 = {target}")

    # 画点
    for idx, val, status, rules in points:
        marker, color, _ = style_map[status]
        ax.plot(idx, val, marker=marker, color=color, markersize=10,
                markeredgecolor="black" if color != "black" else "none",
                markeredgewidth=0.5)

    # 标注失控点
    for idx, val, status, rules in points:
        if status != W.Status.IN_CONTROL:
            label = ",".join(rules) if rules else "?"
            ax.annotate(f"#{idx}\n{label}",
                        xy=(idx, val), xytext=(idx + 0.4, val),
                        fontsize=8, color="darkred",
                        arrowprops=dict(arrowstyle="-", color="gray", lw=0.5))

    ax.set_xlabel("数据点序号（按时间）")
    ax.set_ylabel("测定值")
    ax.set_title(
        f"Levey-Jennings 控制图（靶值={target}, SD={sd}）\n"
        "触发规则展示：1_3s / 2_2s / R_4s / 4_1s / 10_x / 1_2s"
    )
    # 图例（手动）
    from matplotlib.lines import Line2D
    legend_items = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="black",
               markersize=10, label="在控"),
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#f4c430",
               markersize=11, label="警告(1_2s)"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="red",
               markersize=10, label="失控"),
        Line2D([0], [0], color="green", linewidth=1.5, label="中心线"),
        Line2D([0], [0], color="green", linestyle="--", label="±1SD"),
        Line2D([0], [0], color="#f4c430", linestyle="--", label="±2SD"),
        Line2D([0], [0], color="red",  linestyle="-",  label="±3SD"),
    ]
    ax.legend(handles=legend_items, loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def westgard_demo() -> None:
    banner("功能 D Westgard 6 条规则 Levey-Jennings 图演示")

    target, sd = 100.0, 2.0

    # 30 个数据点，靶值 100，SD 2
    # 故意埋 6 条规则：
    #   1) #5  = 105.0  → |sd|=2.5  → 1_2s（警告）
    #   2) #8  = 107.5  → |sd|=3.75 → 1_3s（失控）
    #   3) #11 = 105.5  → |sd|=2.75 → 2_2s（同水平连续 2 条 #8+#11 同方向超 2SD）
    #   4) #14 = 100.0  → 在控
    #   5) #15 = 100.0  → 在控
    #   6) #16 = 100.0  → 在控
    #   7) #17-#20 连续 4 条同方向超 1SD → 4_1s
    #   8) #21-#30 连续 10 条同方向（正侧）→ 10_x
    #  R_4s：同日内跨水平 ±2SD → 需双水平，本图只画单水平就不演示
    values = [
        100.5,  # 1
        99.5,   # 2
        100.0,  # 3
        100.3,  # 4
        105.0,  # 5  → 1_2s (|sd|=2.5)
        99.8,   # 6
        100.2,  # 7
        107.5,  # 8  → 1_3s (|sd|=3.75) + 1_2s + 2_2s (与 #5 同方向超 2SD) 触发
        99.7,   # 9
        100.1,  # 10
        105.5,  # 11 → 2_2s (与 #5/#8 同方向超 2SD) 触发
        100.0,  # 12
        99.9,   # 13
        100.0,  # 14
        100.0,  # 15
        100.0,  # 16
        102.5,  # 17 → 4_1s 第 1 条 (|sd|=1.25)
        102.5,  # 18 → 4_1s 第 2 条
        102.5,  # 19 → 4_1s 第 3 条
        102.5,  # 20 → 4_1s 第 4 条 触发
        100.5,  # 21 → 10_x 同侧
        100.5,  # 22
        100.5,  # 23
        100.5,  # 24
        100.5,  # 25
        100.5,  # 26
        100.5,  # 27
        100.5,  # 28
        100.5,  # 29
        100.5,  # 30 → 10_x 第 10 条 触发
    ]

    print(f"\n  数据：30 个点，靶值={target}, SD={sd}")
    print(f"  故意埋的 6 条规则触发点：")
    print(f"    #5   = 105.0  → 1_2s 警告（|sd|=2.5）")
    print(f"    #8   = 107.5  → 1_3s + 2_2s 失控（|sd|=3.75）")
    print(f"    #11  = 105.5  → 2_2s 失控（连续 2 同方向超 2SD）")
    print(f"    #17-#20 连续 4 条 102.5  → 4_1s 失控（连续 4 同方向超 1SD）")
    print(f"    #21-#30 连续 10 条 100.5  → 10_x 失控（连续 10 同侧）")
    print(f"  （R_4s 需要同日内跨水平 ±2SD，单水平图不演示）")

    print(f"\n  逐条判定结果：")
    print(f"  {'#':>3}  {'值':>6}  {'|SD|':>6}  {'状态':<14}  规则")
    print(f"  {'-'*3}  {'-'*6}  {'-'*6}  {'-'*14}  {'-'*20}")
    history: list[float] = []
    for i, v in enumerate(values):
        res = W.evaluate_measurement(v, target, sd, history)
        sd_s = W.sd_score(v, target, sd)
        sd_label = f"{abs(sd_s):.2f}"
        status_text = {
            W.Status.IN_CONTROL: "在控",
            W.Status.WARNING: "警告",
            W.Status.OUT_OF_CONTROL: "失控",
        }[res["status"]]
        rules = ",".join(res["rules"]) if res["rules"] else "—"
        print(f"  {i+1:>3}  {v:>6.1f}  {sd_label:>6}  {status_text:<14}  {rules}")
        history.append(v)

    out = ROOT / "demos" / "lj_chart.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    draw_lj_chart(values, target, sd, out)
    print(f"\n  Levey-Jennings 图已保存：{out}")


if __name__ == "__main__":
    crud_demo()
    westgard_demo()
    print("\n  ✅ 演示完成\n")
