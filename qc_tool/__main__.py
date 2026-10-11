"""
qc_tool 包入口：启动功能 B 主窗口。

调用方式：
    python -m qc_tool

功能：调 main_window_b.main()
- 启动时 backup.startup_check_and_backup
- 启动后 backup.mark_exit_dirty
- 正常退出时 backup.mark_exit_clean（aboutToQuit 钩）
"""
from __future__ import annotations

import sys


def main() -> int:
    from .main_window_b import main as main_b
    return main_b(sys.argv)


if __name__ == "__main__":
    sys.exit(main())