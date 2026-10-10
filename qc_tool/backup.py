"""
备份策略 —— spec V1.3 §8.2。

行为约定（spec §8.2 原文 + 测试断言）：
- 正常退出：每天 1 份，保留 7 天；文件名 qc_YYYYMMDD.db；
- 同一天内多次 backup_today() 只产生 1 份（idempotent within a day）；
- 异常退出：state.json 标记 last_exit_clean=false，下次启动 startup_check_and_backup
  会多生成 1 份带时间戳的额外备份 qc_YYYYMMDD_HHMMSS.db，与同日正常份共存；
- 提供"从备份恢复"菜单入口 —— 本期只给 UI 描述符 + 抛 RestoreNotImplemented
  占位异常，恢复逻辑不在本期实现范围（用户明确"先做空壳"）；
- state.json 缺失/损坏一律按干净启动处理（不抛、不阻断启动）；
- startup_check_and_backup 不主动清回 state.json；清回干净必须由正常退出钩子
  显式调用 mark_exit_clean（语义：连续异常退出不会丢一次备份）。

不依赖 PyQt6：本模块纯函数 + 文件 I/O，菜单项描述符是一个 dict（label/handler），
UI 层用 QAction 包。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATE_FILENAME = "state.json"     # 与 qc.db 同目录，记录上次退出是否干净
BACKUP_DIRNAME = "backup"         # 备份目录名（qc.db 同级）
KEEP_DAYS = 7                     # spec §8.2：保留最近 7 天


class RestoreNotImplemented(NotImplementedError):
    """占位异常：本期未实现恢复逻辑，UI 层捕获后通常弹"功能建设中"提示框。"""


# ---------------------------------------------------------------------------
# 私有工具
# ---------------------------------------------------------------------------

def _data_dir_of(db_path: Path) -> Path:
    """qc.db 所在目录 = state.json / backup/ 的位置。"""
    return db_path.parent


def _today_str(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("%Y%m%d")


def _read_state(data_dir: Path) -> dict[str, Any]:
    """
    读取 state.json；缺失/损坏一律返回 {"last_exit_clean": True}，不抛。
    """
    p = data_dir / STATE_FILENAME
    if not p.exists():
        return {"last_exit_clean": True}
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {"last_exit_clean": True}
        # last_exit_clean 字段若不是 bool，按干净处理（容错）
        if "last_exit_clean" in data and not isinstance(data["last_exit_clean"], bool):
            data["last_exit_clean"] = True
        return data
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"last_exit_clean": True}


def _write_state(data_dir: Path, payload: dict[str, Any]) -> None:
    """写 state.json；测试也需要这个能力（test_backup 中显式 _write_state）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / STATE_FILENAME
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _prune_old_backups(backup_dir: Path, keep_days: int = KEEP_DAYS) -> None:
    """按 mtime 删除早于 keep_days 天的备份（不含今日）。"""
    if not backup_dir.exists():
        return
    cutoff = datetime.now().timestamp() - keep_days * 86400
    for f in backup_dir.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            # 单文件删不动就跳过，不阻断备份主流程
            pass


# ---------------------------------------------------------------------------
# 公开 API —— 备份
# ---------------------------------------------------------------------------

def backup_today(db_path: str | Path, force_extra: bool = False) -> Path | None:
    """
    把 qc.db 备份到 <data_dir>/backup/ 下。

    - 正常调用：当日首次返回新路径 qc_YYYYMMDD.db；同日第二次返回 None
      （不重复备份，spec §8.2 "一天 1 份"）；
    - force_extra=True：强制多备一份 qc_YYYYMMDD_HHMMSS.db（带秒级时间戳），
      与同日正常份共存，用于"上次异常退出补救"场景；
    - 同时清理 > KEEP_DAYS 天的旧备份；
    - db_path 不存在 → 抛 FileNotFoundError（让上层决定是首次启动跳过还是报错）。

    返回值：新创建的备份路径；同日已存在且未 force_extra 时返回 None。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    data_dir = _data_dir_of(db_path)
    backup_dir = data_dir / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    today = _today_str()
    today_file = backup_dir / f"qc_{today}.db"

    if force_extra:
        stamp = datetime.now().strftime("%H%M%S")
        target = backup_dir / f"qc_{today}_{stamp}.db"
        shutil.copy2(db_path, target)
        _prune_old_backups(backup_dir)
        return target

    if today_file.exists():
        # 同日已备份过：仅清理旧文件，不重复 copy
        _prune_old_backups(backup_dir)
        return None

    shutil.copy2(db_path, today_file)
    _prune_old_backups(backup_dir)
    return today_file


# ---------------------------------------------------------------------------
# 公开 API —— 启动 / 退出钩子
# ---------------------------------------------------------------------------

def startup_check_and_backup(db_path: str | Path) -> list[Path]:
    """
    程序启动时调用：根据上次退出状态决定是否多备一份。

    - db_path 不存在（首次启动，spec §8.3 b） → 返回 []，不报错；
    - state.json last_exit_clean = True（或缺失/损坏）→ 仅做当日正常备份，返回 1 个；
    - state.json last_exit_clean = False → 当日正常备份 + 1 份带时间戳额外备份，
      返回 ≥2 个；
    - **不清回 state.json**：清回必须由正常退出钩子显式调用 mark_exit_clean。

    返回值：本次启动新生成的备份路径列表。
    """
    db_path = Path(db_path)
    data_dir = _data_dir_of(db_path)

    # 首次启动：库都还没建，不应阻断 UI 启动
    if not db_path.exists():
        return []

    results: list[Path] = []

    # 当日正常备份（同日多次调用只会产生 1 份）
    normal = backup_today(db_path)
    if normal is not None:
        results.append(normal)

    # 上次异常退出 → 多备 1 份
    state = _read_state(data_dir)
    if state.get("last_exit_clean") is False:
        extra = backup_today(db_path, force_extra=True)
        if extra is not None:
            results.append(extra)

    return results


def mark_exit_dirty(data_dir: str | Path) -> None:
    """程序启动后立即调用：标记"本次运行可能异常退出"，用于下次启动补救。"""
    data_dir = Path(data_dir)
    _write_state(data_dir, {"last_exit_clean": False})


def mark_exit_clean(data_dir: str | Path) -> None:
    """程序正常退出前调用：标记"本次运行干净"，下次启动不需要额外备份。"""
    data_dir = Path(data_dir)
    _write_state(data_dir, {"last_exit_clean": True})


def is_last_exit_clean(data_dir: str | Path) -> bool:
    """
    读取上次退出状态。
    - state.json 不存在 → True（视为干净，不阻塞启动）；
    - state.json 损坏 → True（同上，容错）；
    - 字段缺失/类型不对 → True。
    """
    return _read_state(Path(data_dir)).get("last_exit_clean", True) is True


# ---------------------------------------------------------------------------
# 公开 API —— 恢复（空壳）
# ---------------------------------------------------------------------------

def restore_from_backup(backup_path: str | Path, target_db_path: str | Path) -> Path:
    """
    从备份恢复到目标 db 路径。

    本期占位：抛 RestoreNotImplemented。恢复涉及"覆盖当前库前的确认对话框""
    关闭 db 连接""备份当前库"等流程，需要 UI 层（spec §8.2 "提供菜单"），待
    UI 阶段统一实现。明确不实现：AGENTS.md 铁律 6 + 用户本次"先做空壳"。
    """
    raise RestoreNotImplemented(
        "本期未实现从备份恢复，UI 阶段接 QAction 后弹'功能建设中'提示。"
    )


def restore_menu_descriptor() -> dict[str, Any]:
    """
    返回菜单项描述符（纯数据，不依赖 PyQt6）。

    UI 层拿到后用 QAction 包一下即可：
        action = QAction(desc["label"], parent)
        action.triggered.connect(desc["handler"])

    handler 当前抛 RestoreNotImplemented，UI 层通常捕获后弹 QMessageBox。
    """
    return {
        "label": "从备份恢复…",
        "handler": _restore_handler,
    }


def _restore_handler(*args: Any, **kwargs: Any) -> Path:
    """菜单点击时调用。占位实现：抛 RestoreNotImplemented。"""
    # 参数被 UI 层忽略；这里只用默认路径占位
    raise RestoreNotImplemented(
        "本期未实现从备份恢复，UI 阶段接 QAction 后弹'功能建设中'提示。"
    )


# 显式导出供测试使用
__all__ = [
    "STATE_FILENAME",
    "BACKUP_DIRNAME",
    "KEEP_DAYS",
    "RestoreNotImplemented",
    "backup_today",
    "startup_check_and_backup",
    "mark_exit_dirty",
    "mark_exit_clean",
    "is_last_exit_clean",
    "restore_from_backup",
    "restore_menu_descriptor",
    "_write_state",
    "_read_state",
]