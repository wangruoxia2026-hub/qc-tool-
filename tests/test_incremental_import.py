"""
增量导入自动化测试（spec V1.3 §B.2 扩展：文件上传支持增量导入）
测试先行：验证导入时自动跳过库中已存在记录，仅入库新增数据，并正确触发 Westgard 规则判定。
"""
from __future__ import annotations

import io
import json
import sqlite3
import pytest
from pathlib import Path

from qc_tool.db import init_database, get_connection
from qc_tool import excel_io as X
from qc_tool import measurements as M
from qc_tool import projects as P


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "qc.db"
    init_database(db)
    c = get_connection(db)
    c.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, NULL)")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _seed_project_and_items(conn: sqlite3.Connection):
    P.set_lab_name(conn, "测试检验科")
    proj = P.create_project(conn, name="生化五项")
    ti_l = P.create_test_item(
        conn, project_id=proj.id, name="ALT", level="L",
        lot="LOT2026A", target=10.0, sd=1.0,
    )
    ti_h = P.create_test_item(
        conn, project_id=proj.id, name="ALT", level="H",
        lot="LOT2026A", target=50.0, sd=3.0,
    )
    return {
        "project_id": proj.id,
        "project_name": proj.name,
        "ti_l_id": ti_l.id,
        "ti_h_id": ti_h.id,
    }


class TestIncrementalImport:
    """增量导入功能测试：
    1. 纯新增数据：全部入库；
    2. 部分重复数据：已存在的跳过，新增的入库；
    3. 全部重复数据：全部跳过，不报错；
    4. 增量导入后对新增点触发 Westgard 规则判定；
    5. 预览接口能够正确区分新增与重复行。
    """

    def test_01_纯新增数据增量导入(self, conn):
        meta = _seed_project_and_items(conn)
        rows = [
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.1, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "H", "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 50.2, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-02", "测定值": 10.2, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "H", "批号": "LOT2026A", "日期": "2026-10-02", "测定值": 50.3, "序号": 1},
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)

        res = X.import_rows(
            conn, plan,
            project_resolver=lambda n: meta["project_id"],
            skip_duplicates=True,
        )
        assert res["imported_count"] == 4
        assert res["duplicates"] == 0
        assert res.get("skipped_duplicates", 0) == 0

        # DB 中应有 4 条
        db_rows = conn.execute("SELECT * FROM measurements").fetchall()
        assert len(db_rows) == 4

    def test_02_部分重复数据增量导入_只入库新增行(self, conn):
        meta = _seed_project_and_items(conn)
        # 先手动录入 10-01 的数据（L 和 H 各 1 条）
        M.create_measurement(
            conn, test_item_id=meta["ti_l_id"], project_id=meta["project_id"],
            lot="LOT2026A", level="L", date="2026-10-01", value=10.0, seq=1,
        )
        M.create_measurement(
            conn, test_item_id=meta["ti_h_id"], project_id=meta["project_id"],
            lot="LOT2026A", level="H", date="2026-10-01", value=50.0, seq=1,
        )

        # 此时上传一个包含了 10-01（旧数据）和 10-02（新数据）的 Excel
        rows = [
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.0, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "H", "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 50.0, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-02", "测定值": 10.3, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "H", "批号": "LOT2026A", "日期": "2026-10-02", "测定值": 50.4, "序号": 1},
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)

        res = X.import_rows(
            conn, plan,
            project_resolver=lambda n: meta["project_id"],
            skip_duplicates=True,
        )
        # 结果应为：导入 2 条（10-02），跳过重复 2 条（10-01）
        assert res["imported_count"] == 2
        assert res["duplicates"] == 2
        assert res["skipped_duplicates"] == 2

        # DB 中总共有 4 条记录
        db_rows = conn.execute("SELECT * FROM measurements").fetchall()
        assert len(db_rows) == 4
        dates = [r["date"] for r in db_rows]
        assert dates.count("2026-10-01") == 2
        assert dates.count("2026-10-02") == 2

    def test_03_全部重复数据增量导入_不抛异常且导入0条(self, conn):
        meta = _seed_project_and_items(conn)
        rows = [
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.0, "序号": 1},
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan1 = X.parse_import_xlsx_bytes(xlsx)
        X.import_rows(conn, plan1, project_resolver=lambda n: meta["project_id"], skip_duplicates=True)

        # 再次上传一模一样的文件
        plan2 = X.parse_import_xlsx_bytes(xlsx)
        res = X.import_rows(
            conn, plan2,
            project_resolver=lambda n: meta["project_id"],
            skip_duplicates=True,
        )
        assert res["imported_count"] == 0
        assert res["duplicates"] == 1
        assert res["skipped_duplicates"] == 1
        # DB 中仍然只有 1 条
        assert len(conn.execute("SELECT * FROM measurements").fetchall()) == 1

    def test_04_增量导入触发新增数据的Westgard判定(self, conn):
        meta = _seed_project_and_items(conn)
        # 10-01 在控
        M.create_measurement(
            conn, test_item_id=meta["ti_l_id"], project_id=meta["project_id"],
            lot="LOT2026A", level="L", date="2026-10-01", value=10.0, seq=1,
        )

        # 增量导入：10-01（旧） + 10-02（新，高偏离 +4.0SD）
        rows = [
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.0, "序号": 1},
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-02", "测定值": 14.0, "序号": 1},  # 10 + 4*1 = 14 -> 1_3s
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)

        res = X.import_rows(
            conn, plan,
            project_resolver=lambda n: meta["project_id"],
            skip_duplicates=True,
        )
        assert res["imported_count"] == 1
        assert res["skipped_duplicates"] == 1

        # 检查新插入的记录状态为失控
        m = conn.execute("SELECT * FROM measurements WHERE date = '2026-10-02'").fetchone()
        assert m is not None
        assert "失控" in m["status"]
        assert "1_3s" in m["rules"]

    def test_05_预览接口标记重复与新增行(self, conn):
        meta = _seed_project_and_items(conn)
        # 先录入 10-01 的 L
        M.create_measurement(
            conn, test_item_id=meta["ti_l_id"], project_id=meta["project_id"],
            lot="LOT2026A", level="L", date="2026-10-01", value=10.0, seq=1,
        )

        rows = [
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-01", "测定值": 10.0, "序号": 1},  # 已存在
            {"项目名": "生化五项", "检测指标": "ALT", "水平": "L", "批号": "LOT2026A", "日期": "2026-10-02", "测定值": 10.5, "序号": 1},  # 新增
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)

        # 模拟 web API 的 preview 处理逻辑
        plan = X.parse_import_xlsx_bytes(xlsx)
        existing_items = conn.execute(
            "SELECT id, name, level, lot FROM test_items WHERE project_id = ?",
            (meta["project_id"],),
        ).fetchall()
        existing_map = {(row["name"], row["level"]): row["id"] for row in existing_items}

        ok_rows_data = []
        for r in plan.ok_rows:
            ti_id = existing_map.get((r.test_item_name, r.level))
            existing_m = conn.execute(
                "SELECT 1 FROM measurements WHERE test_item_id = ? AND lot = ? AND level = ? AND date = ? AND seq = ? LIMIT 1",
                (ti_id, r.lot, r.level, r.date, r.seq),
            ).fetchone()
            ok_rows_data.append({
                "original_row_no": r.original_row_no,
                "is_duplicate": existing_m is not None,
            })

        assert len(ok_rows_data) == 2
        assert ok_rows_data[0]["is_duplicate"] is True
        assert ok_rows_data[1]["is_duplicate"] is False
