"""
qc_tool 包入口：默认起 Web（spec V1.3 全 7 功能）。

调用方式（默认 web）：
    python -m qc_tool
    # → 起 web server，浏览器自动打开 http://127.0.0.1:8765/

兼容旧 exe 启动方式：
    python -m qc_tool --exe
    # → 仍然起 PyQt6 MainWindow（用于老脚本/调试）

设计要点（Step 0，2026-10-11）：
- 默认 web 是"全 spec web 化"的方向决定（用户 2026-10-11 拍板）
- --exe 保留向后兼容，老脚本/IDE 调试不破
- 业务模块 0 修改（ui_main.py / main_window_b.py 等都不动）
- 备份钩子两条路径都保留（spec §8.2）

本入口参考：
- web.main() 实现见 qc_tool/web.py
- ui_main.main() 实现见 qc_tool/ui_main.py
"""
from __future__ import annotations

import sys


def _run_web() -> int:
    """默认入口：起 web server（spec 全 7 功能 + §10 限制）。"""
    from . import web
    # 传 sys.argv 全量，web.main() 内部会自己 argv[1:]（接口契约）
    return web.main(sys.argv)


def _run_exe() -> int:
    """兼容入口：起 PyQt6 MainWindow（保留老脚本/调试）。"""
    from PyQt6.QtWidgets import QApplication
    from .db import get_connection, init_database
    from . import backup
    from .ui_main import (
        MainWindow,
        _default_db_path,
        _ensure_database,
        _ensure_lab_name,
    )

    argv = sys.argv
    app = QApplication.instance() or QApplication(argv)

    db_path = _default_db_path()
    conn = _ensure_database(db_path)

    backup.startup_check_and_backup(db_path)
    backup.mark_exit_dirty(db_path.parent)

    win = MainWindow(conn)
    win.show()
    _ensure_lab_name(conn, parent=win)
    if hasattr(win, "_refresh_status_label"):
        win._refresh_status_label()

    app.aboutToQuit.connect(lambda: backup.mark_exit_clean(db_path.parent))
    return app.exec()


def main() -> int:
    # 解析启动参数（仅识别 --exe / --help，其余透传给 web）
    if "--exe" in sys.argv:
        sys.argv.remove("--exe")
        return _run_exe()
    if "--help" in sys.argv or "-h" in sys.argv:
        print("qc_tool 入口（spec V1.3）")
        print("  默认：python -m qc_tool  →  起 web server（推荐）")
        print("  兼容：python -m qc_tool --exe  →  起 PyQt6 MainWindow")
        print("  web 选项：python -m qc_tool --port 9000 --no-browser")
        return 0
    return _run_web()


if __name__ == "__main__":
    sys.exit(main())
