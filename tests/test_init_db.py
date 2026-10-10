"""
建表正确性测试 —— 对应 spec §8.3 验收。

关注点：
1. 5 张表都建出来了
2. PRAGMA foreign_keys = ON 真的生效（CASCADE / SET NULL 行为）
3. UNIQUE 约束生效
4. spec §8.3 c：删除检测指标 / 删除项目的级联 / 置空行为符合预期
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qc_tool.db import init_database, get_connection


EXPECTED_TABLES = {"projects", "test_items", "measurements", "out_of_control", "lab_settings"}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_init_creates_all_five_tables(tmp_path: Path):
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        assert _tables(conn) == EXPECTED_TABLES
    finally:
        conn.close()


def test_init_is_idempotent(tmp_path: Path):
    """重复执行不报错、不会丢表。"""
    db = tmp_path / "qc.db"
    init_database(db)
    init_database(db)
    init_database(db)
    conn = get_connection(db)
    try:
        assert _tables(conn) == EXPECTED_TABLES
    finally:
        conn.close()


def test_spec_columns_present(tmp_path: Path):
    """关键字段（spec §8.3 c / §8.1）必须存在。"""
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        # out_of_control 必须有 test_item_name / project_name 冗余字段（§8.3 c、§8.3 d）
        assert "test_item_name" in _columns(conn, "out_of_control")
        assert "project_name" in _columns(conn, "out_of_control")
        # test_items.source 默认 = '固定'（§8.1 / §C.3）
        assert "source" in _columns(conn, "test_items")
        # measurements 双重外键字段都在（§8.1 / V1.3 改）
        for col in ("test_item_id", "project_id"):
            assert col in _columns(conn, "measurements")
    finally:
        conn.close()


def test_unique_name_on_projects(tmp_path: Path):
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        conn.execute("INSERT INTO projects(name) VALUES ('维生素 D')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO projects(name) VALUES ('维生素 D')")
    finally:
        conn.close()


def test_unique_test_item_combo(tmp_path: Path):
    """UNIQUE(project_id, name, lot, level) —— 同项目+同指标+水平+批号唯一"""
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        conn.execute("INSERT INTO projects(name) VALUES ('维生素 D')")
        conn.execute(
            "INSERT INTO test_items(project_id, name, lot, level, target, sd) "
            "VALUES (1, 'D2', 'LOT1', 'L', 10.0, 1.0)"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO test_items(project_id, name, lot, level, target, sd) "
                "VALUES (1, 'D2', 'LOT1', 'L', 11.0, 1.0)"
            )
    finally:
        conn.close()


def test_default_source_is_fixed(tmp_path: Path):
    """test_items.source 默认 = '固定'（spec §C.3 / §8.1）"""
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        conn.execute("INSERT INTO projects(name) VALUES ('维生素 D')")
        conn.execute(
            "INSERT INTO test_items(project_id, name, lot, level, target, sd) "
            "VALUES (1, 'D2', 'LOT1', 'L', 10.0, 1.0)"
        )
        conn.commit()
        v = conn.execute("SELECT source FROM test_items WHERE id = 1").fetchone()[0]
        assert v == "固定"
    finally:
        conn.close()


def test_pragma_foreign_keys_on_each_connection(tmp_path: Path):
    """PRAGMA foreign_keys 是 per-connection 的，每次 get_connection 都必须设。"""
    db = tmp_path / "qc.db"
    init_database(db)
    c1 = get_connection(db)
    c2 = get_connection(db)
    try:
        for c in (c1, c2):
            v = c.execute("PRAGMA foreign_keys").fetchone()[0]
            assert v == 1, f"foreign_keys 应为 1，实际为 {v}"
    finally:
        c1.close()
        c2.close()


# ---------- spec §8.3 c：CASCADE / SET NULL 行为 ----------

def _seed_full_chain(conn: sqlite3.Connection) -> dict:
    """插入 project → test_item → measurement → out_of_control，返回关键 id。"""
    conn.execute("INSERT INTO projects(name) VALUES ('维生素 D')")
    conn.execute(
        "INSERT INTO test_items(project_id, name, lot, level, target, sd) "
        "VALUES (1, 'D2', 'LOT1', 'L', 10.0, 1.0)"
    )
    conn.execute(
        "INSERT INTO measurements(test_item_id, project_id, lot, level, date, value) "
        "VALUES (1, 1, 'LOT1', 'L', '2026-04-01', 15.0)"
    )
    conn.execute(
        "INSERT INTO out_of_control("
        "  measurement_id, test_item_id, project_id,"
        "  lot, level, triggered_date,"
        "  test_item_name, project_name, rules, conclusion"
        ") VALUES (1, 1, 1, 'LOT1', 'L', '2026-04-01', 'D2', '维生素 D', '[\"1_3s\"]', '复测合格')"
    )
    conn.commit()
    return {"project_id": 1, "test_item_id": 1, "measurement_id": 1, "ooc_id": 1}


def test_delete_test_item_cascade_measurement_and_null_ooc(tmp_path: Path):
    """
    spec §8.3 c：
    - 删检测指标 → measurements CASCADE
    - out_of_control.measurement_id SET NULL
    - out_of_control.test_item_id 置空（注意：spec §8.1 没给 test_item_id 加 FK，
      SQLite 不会自动置空，必须由业务层/触发器负责；本测试只验证当前行为——
      注意：spec §8.1 注释说"test_item_id 和 project_id 都不加外键，因为删除
      检测指标/项目时需保留失控记录"——意思是"不通过外键 SET NULL 自动化，
      由上层显式置空"。所以删 test_item 后 test_item_id 应保持原值，
      但 measurement_id 因有 FK ON DELETE SET NULL 会被置空。
    - lot/level/triggered_date/rules/conclusion 保留
    - test_item_name 保留
    """
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        ids = _seed_full_chain(conn)

        # 删 test_item → 应触发 measurements CASCADE
        conn.execute("DELETE FROM test_items WHERE id = ?", (ids["test_item_id"],))
        conn.commit()

        # measurements 已 CASCADE 删除
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0

        # out_of_control.measurement_id 因 FK ON DELETE SET NULL 被置空
        row = conn.execute(
            "SELECT measurement_id, test_item_id, project_id,"
            "       lot, level, triggered_date, rules, conclusion,"
            "       test_item_name, project_name "
            "FROM out_of_control WHERE id = ?",
            (ids["ooc_id"],),
        ).fetchone()
        assert row[0] is None, "measurement_id 应被 SET NULL"
        # test_item_id / project_id 没有 FK，不会自动置空（spec 设计如此）
        # 但失控上下文核心字段必须保留
        assert row[3] == "LOT1"
        assert row[4] == "L"
        assert row[5] == "2026-04-01"
        assert row[6] == '["1_3s"]'
        assert row[7] == "复测合格"
        # 冗余 name 字段保留（前端可显示历史名）
        assert row[8] == "D2"
        assert row[9] == "维生素 D"
    finally:
        conn.close()


def test_delete_project_cascades_measurements(tmp_path: Path):
    """
    spec §8.3 c：删项目 → measurements CASCADE、out_of_control.measurement_id SET NULL
    """
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        ids = _seed_full_chain(conn)
        conn.execute("DELETE FROM projects WHERE id = ?", (ids["project_id"],))
        conn.commit()

        # measurements CASCADE
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
        # out_of_control.measurement_id SET NULL
        v = conn.execute(
            "SELECT measurement_id FROM out_of_control WHERE id = ?", (ids["ooc_id"],)
        ).fetchone()[0]
        assert v is None
    finally:
        conn.close()


def test_init_db_cli_module_importable():
    """init_db CLI 模块必须可导入（给 python -m qc_tool.init_db 用）。"""
    import qc_tool.init_db  # noqa: F401

    assert hasattr(qc_tool.init_db, "main")