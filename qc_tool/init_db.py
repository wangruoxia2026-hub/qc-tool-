"""
建库 CLI：在 qc.db 不存在时建空库，已存在则不动（IF NOT EXISTS 语义）。

用法：
    python -m qc_tool.init_db                    # 默认建在程序所在目录的 qc.db
    python -m qc_tool.init_db --db-path D:/xxx/qc.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import init_database


def _default_db_path() -> Path:
    """默认 db 路径：包所在目录（即程序根目录）下的 qc.db。"""
    return Path(__file__).resolve().parent.parent / "qc.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 spec §8.1 建库")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_default_db_path(),
        help="数据库文件路径（默认：程序根目录/qc.db）",
    )
    args = parser.parse_args(argv)

    db_path: Path = args.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_database(db_path)
    print(f"OK: 已按 spec §8.1 建库 -> {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())