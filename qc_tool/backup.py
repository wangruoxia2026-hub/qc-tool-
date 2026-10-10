"""
备份策略（spec §8.2）。

规则：
- 正常退出：每天 1 份，保留 7 天
- 异常退出：下次启动检测到，再多备份 1 份（带时间戳以与当日正常份共存）
- 备份目录 backup/，文件名 qc_YYYYMMDD.db
- 异常退出检测：用 db_path 同目录下的 state.json，记录 last_exit_clean 布尔值
  - 启动时检查 state.json，若 last_exit_clean = false → 多备 1 份 + 不改标志
    （让用户在"下次正常退出"时才清回 true，符合"下次启动检测"语义）
  - 正常退出钩子里调 mark_exit_clean(db_dir)

本期范围外：
- 从备份恢复：仅注册菜单项入口，函数抛 NotImplementedError，UI 层包 QMessageBox
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path


# ---------- 常量 ----------
BACKUP_DIRNAME = "backup"
KEEP_DAYS = 7
STATE_FILENAME = "state.json"


# ---------- state.json ----------
def _state_path(db_dir: Path) -> Path:
    return db_dir / STATE_FILENAME


def _read_state(db_dir: Path) -> dict:
    """读取 state.json，不存在返回 last_exit_clean = True（视为干净启动）。"""
    p = _state_path(db_dir)
    if not p.exists():
        return {"last_exit_clean": True}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"last_exit_clean": True}
        return data
    except (json.JSONDecodeError, OSError):
        # 文件损坏也按"干净"处理，避免启动失败
        return {"last_exit_clean": True}


def _write_state(db_dir: Path, data: dict) -> None:
    p = _state_path(db_dir)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def mark_exit_clean(db_dir: Path) -> None:
    """正常退出钩子调用：标记下次启动认为是'干净'，不会再多备 1 份。"""
    _write_state(db_dir, {"last_exit_clean": True})


def mark_exit_dirty(db_dir: Path) -> None:
    """异常退出时调用：标记下次启动需要多备 1 份。"""
    _write_state(db_dir, {"last_exit_clean": False})


def is_last_exit_clean(db_dir: Path) -> bool:
    """启动时调用：上次是否正常退出。"""
    return bool(_read_state(db_dir).get("last_exit_clean", True))


# ---------- 备份 ----------
def _backup_dir(db_dir: Path) -> Path:
    d = db_dir / BACKUP_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _today_name(now: datetime | None = None) -> str:
    t = now or datetime.now()
    return t.strftime("qc_%Y%m%d.db")


def _extra_name(now: datetime | None = None) -> str:
    """异常退出追加份：带时间戳，避免与同日正常份同名冲突。"""
    t = now or datetime.now()
    return t.strftime("qc_%Y%m%d_%H%M%S.db")


def _prune_old_backups(backup_dir: Path, now: datetime | None = None) -> int:
    """删除 mtime 早于 (今天 - KEEP_DAYS 天) 的备份文件。返回删除数量。"""
    t = now or datetime.now()
    threshold = t - timedelta(days=KEEP_DAYS)
    removed = 0
    for f in backup_dir.iterdir():
        if not f.is_file():
            continue
        # 按 mtime 删（spec 写"最近 7 天"，按 mtime 比按文件名解析日期更稳）
        if datetime.fromtimestamp(f.stat().st_mtime) < threshold:
            f.unlink()
            removed += 1
    return removed


def backup_today(
    db_path: Path | str,
    *,
    now: datetime | None = None,
    force_extra: bool = False,
) -> Path | None:
    """
    备份今日份。
    - 默认（force_extra=False）：若 backup/qc_YYYYMMDD.db 已存在则跳过，返回 None
    - force_extra=True：生成 qc_YYYYMMDD_HHMMSS.db，永远新建一份（异常退出追加场景）
    - 每次调用都触发一次 7 天清理

    返回：实际写入的备份路径；同日重名跳过时返回 None。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    db_dir = db_path.parent
    backup_dir = _backup_dir(db_dir)

    target_name = _extra_name(now) if force_extra else _today_name(now)
    target = backup_dir / target_name
    if target.exists():
        # force_extra 路径下不应该撞名（带秒级时间戳），撞了说明同秒并发调用，宁可不覆盖
        return None
    shutil.copy2(db_path, target)

    _prune_old_backups(backup_dir, now)
    return target


def startup_check_and_backup(db_path: Path | str, *, now: datetime | None = None) -> list[Path]:
    """
    启动时调用：
    - 若上次非正常退出，多备份 1 份（force_extra=True）
    - 不管是否异常，都再备份今日份（保证"正常退出时每天一份"也覆盖到启动时这次）
      —— 注意：本次调用不会自动 mark_exit_clean，UI 启动逻辑自己决定何时调用
    - 总是触发一次 7 天清理

    返回：本次新生成的备份文件路径列表（顺序：normal, extra?）
    """
    db_path = Path(db_path)
    if not db_path.exists():
        # 首次启动 db 还没建（spec §8.3 b：删 qc.db 后下次启动自动建空库）
        # 这种情况没有可备份内容
        return []

    db_dir = db_path.parent
    paths: list[Path] = []

    p = backup_today(db_path, now=now, force_extra=False)
    if p is not None:
        paths.append(p)

    if not is_last_exit_clean(db_dir):
        p2 = backup_today(db_path, now=now, force_extra=True)
        if p2 is not None:
            paths.append(p2)

    return paths


# ---------- 菜单项描述 ----------
# 本期不实现"从备份恢复"（spec §8.2 + §10 范围外），但保留菜单入口。
# 设计：backup.py 只产出"应该注册什么"，不依赖 PyQt6；
# UI 层用 PyQt6 把这个 dict 包装成 QAction。这样 CLI / 测试无需装 Qt。

RESTORE_MENU_LABEL = "从备份恢复…"


class RestoreNotImplemented(NotImplementedError):
    """恢复功能本期不实现，仅占位。"""


def restore_from_backup(backup_file: Path | str, target_db_path: Path | str) -> None:
    """从备份恢复。本期不实现，UI 层菜单入口已预留。"""
    raise RestoreNotImplemented(
        "本期不实现'从备份恢复'（spec §8.2），菜单入口已注册为占位项。"
    )


def restore_menu_descriptor() -> dict:
    """
    返回菜单项描述（纯 dict，无 Qt 依赖）：
      {"label": str, "handler": Callable[[], None]}

    UI 层典型用法：
        from PyQt6.QtGui import QAction
        d = backup.restore_menu_descriptor()
        a = QAction(d["label"], menu)
        a.triggered.connect(d["handler"])
        menu.addAction(a)
    """
    def _on_clicked() -> None:
        raise RestoreNotImplemented(
            "本期不实现'从备份恢复'（spec §8.2），菜单入口已注册为占位项。"
        )

    return {"label": RESTORE_MENU_LABEL, "handler": _on_clicked}