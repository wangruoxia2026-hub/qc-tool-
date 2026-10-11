"""
数据迁移：将 source 列加入已存在的 qc.db。

用途：用户升级时，已有 qc.db 的 test_items 表没有 source 列，执行本脚本补上。
幂等：COLUMN 已存在则跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保 qc_tool 在路径中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qc_tool.db import get_connection


def migrate_add_source(db_path: str | Path = "qc.db") -> None:
    db_path = Path(db_path)
    conn = get_connection(db_path)

    # 检查列是否存在（SQLite PRAGMA table_info）
    rows = conn.execute("PRAGMA table_info(test_items)").fetchall()
    col_names = {row["name"] for row in rows}

    if "source" not in col_names:
        conn.execute(
            "ALTER TABLE test_items ADD COLUMN source TEXT DEFAULT '固定'"
        )
        conn.commit()
        print(f"✓ 迁移完成：test_items.source 列已添加（默认值 '固定'）")
    else:
        print(f"✓ 列 source 已存在，无需迁移")

    conn.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "qc.db"
    migrate_add_source(path)
