"""
备份策略测试 —— 对应 spec §8.2。

关注点：
1. 正常退出：一天内多次调用只生成 1 份 qc_YYYYMMDD.db
2. force_extra=True 时生成 qc_YYYYMMDD_HHMMSS.db，与同日正常份共存
3. 8 天前的旧文件被删、今天的留着
4. 异常退出：state.json last_exit_clean=false → startup_check_and_backup 多生成 1 份
5. restore_from_backup 抛 RestoreNotImplemented
6. register_restore_menu_item：menu 是 duck-typed，不强依赖 PyQt6
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from qc_tool import backup


@pytest.fixture
def db_with_state(tmp_path: Path) -> Path:
    """建一个空 qc.db，返回路径。state.json 不预置（视为干净启动）。"""
    db = tmp_path / "qc.db"
    db.write_bytes(b"")  # 内容随便，只是要存在以让 backup 能 copy
    return db


def test_backup_today_creates_one_file(db_with_state: Path):
    p1 = backup.backup_today(db_with_state)
    p2 = backup.backup_today(db_with_state)
    assert p1 is not None
    assert p2 is None, "同日第二次应跳过，返回 None"
    # backup 目录里只有一份
    bdir = db_with_state.parent / "backup"
    assert sorted(bdir.iterdir()) == [p1]


def test_backup_today_filename_pattern(db_with_state: Path):
    p = backup.backup_today(db_with_state)
    assert p is not None
    assert p.name == datetime.now().strftime("qc_%Y%m%d.db")


def test_backup_force_extra_creates_second_file(db_with_state: Path):
    p1 = backup.backup_today(db_with_state)
    p2 = backup.backup_today(db_with_state, force_extra=True)
    assert p1 is not None
    assert p2 is not None
    assert p1 != p2
    # 第二份带时间戳
    assert "_" in p2.stem.replace("qc_", "_").replace(".db", "").replace("20260401", "20260401_")
    # 实际更直白：第二份 stem 含秒级时间戳
    assert p2.name.startswith(datetime.now().strftime("qc_%Y%m%d_"))
    assert p2.name.endswith(".db")
    # 两份并存
    bdir = db_with_state.parent / "backup"
    files = sorted(bdir.iterdir())
    assert p1 in files and p2 in files


def test_backup_raises_if_db_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        backup.backup_today(tmp_path / "nonexistent.db")


def test_old_backups_pruned(db_with_state: Path):
    """8 天前的备份应被删；今天的留着。"""
    bdir = db_with_state.parent / "backup"
    bdir.mkdir(parents=True, exist_ok=True)

    today = datetime.now()
    old_date = today - timedelta(days=8)
    old_name = old_date.strftime("qc_%Y%m%d.db")
    old_file = bdir / old_name
    old_file.write_bytes(b"old")

    # 把 mtime 改成 8 天前（write_bytes 默认 mtime 是 now）
    import os
    old_mtime = old_date.timestamp()
    os.utime(old_file, (old_mtime, old_mtime))

    # 触发一次备份（同时清理）
    p = backup.backup_today(db_with_state)
    assert p is not None

    assert not old_file.exists(), "8 天前的备份应被清理"
    assert p.exists(), "今天的备份应保留"


def test_startup_normal_exit_no_extra_copy(db_with_state: Path):
    """state.json 默认为干净 → startup 只生成 1 份（正常的当日备份）。"""
    paths = backup.startup_check_and_backup(db_with_state)
    assert len(paths) == 1
    assert paths[0].name == datetime.now().strftime("qc_%Y%m%d.db")


def test_startup_abnormal_exit_makes_extra_copy(db_with_state: Path):
    """上次异常退出（state.json last_exit_clean=false）→ startup 多生成 1 份。"""
    backup._write_state(db_with_state.parent, {"last_exit_clean": False})

    paths = backup.startup_check_and_backup(db_with_state)
    names = sorted(p.name for p in paths)
    today = datetime.now().strftime("qc_%Y%m%d.db")
    # 应该至少有 1 份正常 + 1 份带时间戳的 extra
    assert any(n.startswith(today.replace(".db", "_")) for n in names)
    assert any(n == today for n in names)


def test_startup_abnormal_does_not_clear_state(db_with_state: Path):
    """
    spec §8.2 语义："下次启动时检测到异常，再备份 1 份"
    本次启动备份完了不应自动把状态清回 clean（否则用户连续两次异常就会丢一次备份）。
    清回 clean 必须由 UI 正常退出钩子显式调 mark_exit_clean。
    """
    backup._write_state(db_with_state.parent, {"last_exit_clean": False})
    backup.startup_check_and_backup(db_with_state)
    # 状态保持 false
    assert backup.is_last_exit_clean(db_with_state.parent) is False


def test_mark_exit_clean_and_dirty(db_with_state: Path):
    backup.mark_exit_dirty(db_with_state.parent)
    assert backup.is_last_exit_clean(db_with_state.parent) is False
    backup.mark_exit_clean(db_with_state.parent)
    assert backup.is_last_exit_clean(db_with_state.parent) is True


def test_state_missing_treated_as_clean(tmp_path: Path):
    """state.json 不存在 → 视为干净启动（不报错、不多备）。"""
    assert backup.is_last_exit_clean(tmp_path) is True


def test_state_corrupt_treated_as_clean(tmp_path: Path):
    """state.json 内容损坏 → 不应导致启动失败，按干净处理。"""
    p = tmp_path / "state.json"
    p.write_text("{this is not valid json", encoding="utf-8")
    assert backup.is_last_exit_clean(tmp_path) is True


def test_startup_no_db_returns_empty(tmp_path: Path):
    """首次启动 db 还没建（spec §8.3 b）→ 启动备份应返回空列表，不报错。"""
    paths = backup.startup_check_and_backup(tmp_path / "nope.db")
    assert paths == []


def test_restore_raises_not_implemented(tmp_path: Path):
    with pytest.raises(backup.RestoreNotImplemented):
        backup.restore_from_backup(tmp_path / "any.db", tmp_path / "qc.db")


def test_restore_menu_descriptor_is_pure_data(db_with_state: Path):
    """
    backup.py 不依赖 PyQt6：菜单项描述只是一个 dict，
    UI 层用 QAction 包。测试只验 label + handler 可调用且抛占位异常。
    """
    d = backup.restore_menu_descriptor()
    assert d["label"] == "从备份恢复…"
    # handler 是可调用对象
    handler = d["handler"]
    assert callable(handler)
    # handler 当前抛占位异常（UI 层接上后通常会包成 QMessageBox 提示）
    with pytest.raises(backup.RestoreNotImplemented):
        handler()