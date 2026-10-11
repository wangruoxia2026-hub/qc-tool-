"""
CLI 入口 —— `python -m qc_tool.init_db`。

行为：在当前工作目录（或命令行传入的路径）创建/打开 qc.db 并执行 §8.1 建表。
用途：手动建库 + 冒烟测试（程序启动前先确保库存在）。

本模块不包含任何业务逻辑，所有表结构定义在 qc_tool.db 里。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import init_database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m qc_tool.init_db",
        description="按 spec V1.3 §8.1 建库（projects/test_items/measurements/out_of_control/lab_settings）",
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        default="qc.db",
        help="数据库文件路径，默认 ./qc.db",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db_path).resolve()
    init_database(db_path)
    print(f"数据库已就绪: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())