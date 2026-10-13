# -*- coding: utf-8 -*-
"""
一次性数据修复（2026-10-13，用户拍板批准）：

修复两个 bug 造成的脏数据（qc.db，项目"1"→ ALT，批号 20261001）：
  1. 合并扫描"连坐"→ 全部 102 条误标失控 + 102 条错误 out_of_control 记录；
  2. 导入丢 seq → 同模板导入 3 次（16:39:35 / 16:44:28 / 16:45:39）产生 3 份重复。

修复步骤：
  1. 备份 qc.db → backup/qc_20261013_修复前.db
  2. 删后两份重复导入（measurement id 279–346），保留第一份（245–278）
  3. 按用户确认"L/H 各 1..17"恢复序号（各水平按插入顺序 1..17）
  4. 删除项目 4 全部 out_of_control 记录（已确认均无调查数据，纯 bug 产物）
  5. 用修复后的判定代码重判 2026-10-02 与 2026-10-13（含 19:32 手工录入的
     L=40 / H=1000 两条）
  6. 输出校验摘要

用法：python repair_20261013_seq_ooc.py（在项目根目录执行，使用 ./qc.db）
"""
import shutil
import sqlite3
import sys
from datetime import datetime

DB = "qc.db"
BACKUP = "backup/qc_20261013_修复前.db"

# 保留的第一份导入（2026-10-02，17 L + 17 H），按 id 顺序即模板行顺序
KEEP_L_IDS = list(range(245, 262))   # L：seq 1..17
KEEP_H_IDS = list(range(262, 279))   # H：seq 1..17
DELETE_IDS = list(range(279, 347))   # 第二、三份重复导入


def main() -> int:
    # 0. 预检
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    n = cur.execute(
        "SELECT COUNT(*) FROM measurements WHERE id BETWEEN 279 AND 346").fetchone()[0]
    if n != len(DELETE_IDS):
        print(f"[预检失败] 待删测量数 {n} != {len(DELETE_IDS)}，中止")
        return 1
    n = cur.execute(
        "SELECT COUNT(*) FROM measurements WHERE id BETWEEN 245 AND 278").fetchone()[0]
    if n != 34:
        print(f"[预检失败] 保留测量数 {n} != 34，中止")
        return 1
    inv = cur.execute(
        "SELECT COUNT(*) FROM out_of_control WHERE project_id = 4 AND "
        "(checklist IS NOT NULL OR conclusion IS NOT NULL OR "
        " error_type IS NOT NULL OR reviewed_by IS NOT NULL)").fetchone()[0]
    if inv != 0:
        print(f"[预检失败] 项目 4 有 {inv} 条 OOC 含调查数据，中止（不许误删）")
        return 1

    # 1. 备份
    shutil.copy2(DB, BACKUP)
    print(f"[1/6] 已备份 {DB} -> {BACKUP}")

    # 2. 删后两份重复导入（连同其 OOC 记录）
    cur.execute(
        "DELETE FROM out_of_control WHERE measurement_id BETWEEN 279 AND 346")
    cur.execute("DELETE FROM measurements WHERE id BETWEEN 279 AND 346")
    print(f"[2/6] 已删除重复导入 {len(DELETE_IDS)} 条（279–346）")

    # 3. 恢复序号：L 按 id 顺序 1..17，H 按 id 顺序 1..17
    for seq, mid in enumerate(KEEP_L_IDS, start=1):
        cur.execute("UPDATE measurements SET seq = ? WHERE id = ?", (seq, mid))
    for seq, mid in enumerate(KEEP_H_IDS, start=1):
        cur.execute("UPDATE measurements SET seq = ? WHERE id = ?", (seq, mid))
    print("[3/6] 已恢复序号：L 1..17、H 1..17")

    # 4. 删除项目 4 全部 OOC 记录（纯 bug 产物，无调查数据）
    cur.execute("DELETE FROM out_of_control WHERE project_id = 4")
    print("[4/6] 已删除项目 4 全部 OOC 记录")

    # 5. 用修复后的判定代码重判两个日期
    from qc_tool import measurements as M  # noqa: E402（修复后的代码）
    for d in ("2026-10-02", "2026-10-13"):
        res = M.rescan_day_for_test_item(conn, test_item_id=15, date=d)
        print(f"[5/6] 重判 {d}: rows_updated={res['rows_updated']} "
              f"ooc_inserted={res['ooc_inserted']}")
    conn.commit()

    # 6. 校验摘要
    print("[6/6] 校验：")
    for tid in (15, 16):
        lvl = "L" if tid == 15 else "H"
        for d in ("2026-10-02", "2026-10-13"):
            rows = cur.execute(
                "SELECT status, COUNT(*) c FROM measurements "
                "WHERE test_item_id = ? AND date = ? GROUP BY status "
                "ORDER BY c DESC", (tid, d)).fetchall()
            summary = ", ".join(f"{r['status']}={r['c']}" for r in rows)
            print(f"  {lvl} {d}: {summary}")
    ooc = cur.execute(
        "SELECT COUNT(*) FROM out_of_control WHERE project_id = 4").fetchone()[0]
    print(f"  out_of_control(项目4): {ooc}")
    total = cur.execute(
        "SELECT COUNT(*) FROM measurements WHERE test_item_id IN (15,16)").fetchone()[0]
    print(f"  measurements(ALT): {total}（应为 36 = 34 + 2）")
    conn.close()
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
