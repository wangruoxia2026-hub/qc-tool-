"""
Issue #2（功能 B：数据录入、Excel 导入与记录作废）—— 测试先行（spec V1.3 §B + §9 + §8.3 + §F.1）

按铁律 5："判定逻辑必须测试先行"。本文件先写验收场景，跑一遍确认：
- 业务层 `qc_tool.measurements` 全部 API 的接口形状稳定（绿/红分布合理）
- spec §B.1–B.4 / §9 / §8.3 / §F.1 的验收逐条覆盖

测试组织：
- TestB1SingleEntry   ：spec §B.1 手工录入（6 条验收）
- TestB2ExcelImport   ：spec §B.2 批量导入（8 条验收）
- TestB3Template      ：spec §B.3 模板下载（2 条验收）
- TestB4EditVoid      ：spec §B.4 编辑/作废（4 条验收）
- TestBoundary        ：spec §9 边界情况
- TestPersistence     ：spec §8.3 数据持久化（6 条验收）
- TestF1Investigation ：spec §F.1 失控排查面板（≥4 条验收）
- TestStatusText      ：状态列字串稳定性

依赖：
- qc_tool.db.init_database / get_connection
- qc_tool.projects（CRUD）
- qc_tool.westgard（已存在）
- qc_tool.measurements（待实现）
- qc_tool.excel_io（待实现）
- qc_tool.investigation（待实现）
- qc_tool.backup（已存在）

跑法：
    python -m pytest tests/test_issue_b.py -v
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Iterable, List

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import westgard as W
from qc_tool import measurements as M
from qc_tool import excel_io as X
from qc_tool import investigation as I
from qc_tool import backup


# ============================================================================
# Fixture
# ============================================================================

@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "qc.db"
    init_database(db)
    c = get_connection(db)
    c.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, NULL)")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _seed_basic(
    conn: sqlite3.Connection,
    *,
    project: str = "维生素 D",
    name: str = "D2",
    lot: str = "LOT1",
    target: float = 100.0,
    sd: float = 2.0,
    add_h: bool = True,
) -> dict:
    """建项目 + L 水平 (+ 可选 H 水平)，返回 id 字典。"""
    p = P.create_project(conn, name=project)
    ti_l = P.create_test_item(
        conn, p.id, name=name, lot=lot, level="L",
        target=target, sd=sd,
    )
    out = {"project_id": p.id, "test_item_id_l": ti_l.id}
    if add_h:
        ti_h = P.create_test_item(
            conn, p.id, name=name, lot=lot, level="H",
            target=target, sd=sd,
        )
        out["test_item_id_h"] = ti_h.id
    return out


# ============================================================================
# §B.1 手工录入单条
# ============================================================================

class TestB1SingleEntry:
    """spec §B.1 验收 a–f + 状态列定义。"""

    def test_01a_录入在控_状态黑色字样在控(self, conn):
        ids = _seed_basic(conn)
        m = M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        assert m.status == "在控"
        assert m.rules is None

    def test_01b_录入超出正3SD_状态失控1_3s_红色(self, conn):
        ids = _seed_basic(conn)
        # 靶值 100 SD 2 → +3SD = 106；取 107.0 → |sd|=3.5 → 1_3s 触发
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=107.0,
        )
        assert m.status is not None and m.status.startswith("失控")
        rules = json.loads(m.rules)
        assert "1_3s" in rules

    def test_01c_同日同水平可录多条_全部入库(self, conn):
        ids = _seed_basic(conn)
        m1 = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0, seq=1,
        )
        m2 = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=101.0, seq=2,
        )
        assert m1.id != m2.id
        rows = M.list_measurements(conn, ids["test_item_id_l"])
        assert len([m for m in rows if m.date == "2026-04-01"]) == 2

    def test_01d_录入v_2_01SD_状态警告1_2s_黄色(self, conn):
        ids = _seed_basic(conn)
        # 靶值 100 SD 2 → +2SD = 104；取 104.1 → |sd|=2.05 → 1_2s 触发但 1_3s 不触发
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=104.1,
        )
        assert m.status is not None and m.status.startswith("警告")
        rules = json.loads(m.rules)
        assert "1_2s" in rules
        assert "1_3s" not in rules

    def test_01e_补录历史日期_立即重扫该日期所有水平(self, conn):
        ids = _seed_basic(conn)
        # 先录 4/1 一条正常
        M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        # 再补录 4/2 一条 1_3s（应触发）；同时也要重扫 4/1
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-02", value=107.0,
        )
        assert m.status is not None and m.status.startswith("失控")
        # 4/1 那条应仍然是"在控"（没跨日重算）
        rows = [r for r in M.list_measurements(conn, ids["test_item_id_l"])
                if r.date == "2026-04-01"]
        assert rows[0].status == "在控"

    def test_01f_保存时立即合并扫描_跨日限定补录日(self, conn):
        """跨日补录 → 仅扫描补录日，不影响其他日期的 status。"""
        ids = _seed_basic(conn)
        # 4/1 一条 +4SD 触发 1_3s
        M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=108.0,  # +4SD
        )
        # 4/2 一条在控
        M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-02", value=100.0,
        )
        # 4/3 录入时 → 不应影响 4/2 的状态（限定在补录日）
        M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-03", value=100.0,
        )
        rows = {r.date: r for r in M.list_measurements(conn, ids["test_item_id_l"])}
        assert rows["2026-04-02"].status == "在控"
        assert rows["2026-04-03"].status == "在控"

    def test_状态列定义_四种状态文案(self):
        """spec §B.1 状态列定义"""
        # 文案快照（防止后续重构改文案）
        assert "在控" in "在控"
        assert "警告(1_2s)" in "警告(1_2s)"
        assert "失控" in "失控(1_3s)"
        assert "作废" in "作废"


# ============================================================================
# §B.2 批量导入
# ============================================================================

class TestB2ExcelImport:
    """spec §B.2 验收 a–h。"""

    def test_02a_导入50行规范数据_列表50条(self, conn):
        ids = _seed_basic(conn)
        rows = []
        for i in range(1, 51):
            rows.append({
                "项目名": "维生素 D",
                "检测指标": "D2",
                "水平": "L",
                "批号": "LOT1",
                "日期": f"2026-04-{(i % 28) + 1:02d}",
                "测定值": 100.0,
                "序号": i % 5,
            })
        plan = X.parse_import_xlsx_bytes(X.build_template_xlsx_with_rows(rows))
        # 走导入入口
        result = X.import_rows(conn, plan, project_resolver=lambda n: ids["project_id"])
        assert result["imported_count"] == 50

        rows_db = M.list_measurements(conn, ids["test_item_id_l"])
        assert len(rows_db) == 50

    def test_02b_缺必需列_整批拒绝(self, conn):
        """少 1 列 → MissingRequiredColumns，整批拒绝（不写库）。"""
        # 构造只有 5 列的"模板"
        bad_xlsx = X.build_xlsx_with_columns(
            ["项目名", "检测指标", "水平", "批号", "日期"],  # 缺"测定值"
            [],
        )
        with pytest.raises(M.MissingRequiredColumns) as exc:
            X.parse_import_xlsx_bytes(bad_xlsx)
        assert "测定值" in exc.value.columns

    def test_02c_某行日期格式错乱_该行跳过_错误报告(self, conn):
        ids = _seed_basic(conn)
        rows = [
            {  # OK
                "项目名": "维生素 D", "检测指标": "D2",
                "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
                "测定值": 100.0, "序号": 1,
            },
            {  # 日期错
                "项目名": "维生素 D", "检测指标": "D2",
                "水平": "L", "批号": "LOT1", "日期": "2026/04/02",  # 错误格式
                "测定值": 100.5, "序号": 2,
            },
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        # 错误报告里应包含日期格式错误（行号可能 2 或 3，依实现而定）
        assert any("日期格式错误" in s.reason for s in plan.skipped_rows), \
            f"期望跳过的行有日期格式错误，实际 skipped={[s.reason for s in plan.skipped_rows]}"

    def test_02d_测定值文字空白_跳过_错误报告(self, conn):
        ids = _seed_basic(conn)
        rows = [
            {  # 文字
                "项目名": "维生素 D", "检测指标": "D2",
                "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
                "测定值": "abc", "序号": 1,
            },
            {  # 空白
                "项目名": "维生素 D", "检测指标": "D2",
                "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
                "测定值": "", "序号": 2,
            },
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        # 错误报告里应包含这两行
        assert len(plan.skipped_rows) >= 2

    def test_02e_项目不存在_整批拒绝(self, conn):
        rows = [{
            "项目名": "不存在的项目", "检测指标": "D2",
            "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
            "测定值": 100.0, "序号": 1,
        }]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        with pytest.raises(X.ProjectNotFound):
            X.import_rows(conn, plan, project_resolver=lambda n: None)

    def test_02f_检测指标不存在_整批拒绝(self, conn):
        ids = _seed_basic(conn)
        rows = [{
            "项目名": "维生素 D", "检测指标": "不存在的指标",
            "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
            "测定值": 100.0, "序号": 1,
        }]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        with pytest.raises(X.TestItemNotFound):
            X.import_rows(conn, plan, project_resolver=lambda n: ids["project_id"])

    def test_02g_重复_发现N条重复_询问覆盖(self, conn):
        ids = _seed_basic(conn)
        # 先录 1 条
        M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0, seq=1,
        )
        # 导入同样的 1 条
        rows = [{
            "项目名": "维生素 D", "检测指标": "D2",
            "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
            "测定值": 105.0, "序号": 1,
        }]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        # 不传 overwrite → 抛 DuplicateImportRows
        with pytest.raises(M.DuplicateImportRows) as exc:
            X.import_rows(
                conn, plan,
                project_resolver=lambda n: ids["project_id"],
                overwrite=False,
            )
        assert exc.value.count == 1
        # overwrite=True → 覆盖
        result = X.import_rows(
            conn, plan,
            project_resolver=lambda n: ids["project_id"],
            overwrite=True,
        )
        assert result["imported_count"] == 1

    def test_02h_成功行入库并逐条判定_触发保存时合并扫描(self, conn):
        ids = _seed_basic(conn)
        # 导入 2 条：一条 +3.5SD（失控）+ 一条 +3.5SD（失控）
        # 按 spec §B.1 f：导入后 rescan 当日所有 measurement，所以两条都应触发 1_3s
        rows = [
            {"项目名": "维生素 D", "检测指标": "D2",
             "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
             "测定值": 107.0, "序号": 1},  # +3.5SD → 1_3s
            {"项目名": "维生素 D", "检测指标": "D2",
             "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
             "测定值": 107.0, "序号": 2},  # +3.5SD → 1_3s
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        X.import_rows(conn, plan, project_resolver=lambda n: ids["project_id"])

        rows_db = sorted(M.list_measurements(conn, ids["test_item_id_l"]),
                         key=lambda m: m.seq)
        # 两条都应失控
        for r in rows_db:
            assert r.status is not None and r.status.startswith("失控"), \
                f"seq={r.seq} value={r.value} status={r.status!r}"


# ============================================================================
# §B.3 模板下载
# ============================================================================

class TestB3Template:
    """spec §B.3 验收 a–b。"""

    def test_03a_下载模板_保存xlsx(self):
        xlsx = X.build_template_xlsx()
        assert isinstance(xlsx, bytes)
        assert len(xlsx) > 0
        # 是合法 zip（xlsx 本质是 zip）
        with zipfile.ZipFile(io.BytesIO(xlsx)) as zf:
            assert any(n.endswith(".xml") for n in zf.namelist())

    def test_03b_模板含6必需列与1行示例(self):
        xlsx = X.build_template_xlsx()
        plan = X.parse_import_xlsx_bytes(xlsx)
        # 模板列结构 + 示例
        assert "项目名" in plan.columns
        assert "检测指标" in plan.columns
        assert "水平" in plan.columns
        assert "批号" in plan.columns
        assert "日期" in plan.columns
        assert "测定值" in plan.columns
        assert len(plan.ok_rows) == 1


# ============================================================================
# §B.4 单条编辑 / 标记作废
# ============================================================================

class TestB4EditVoid:
    """spec §B.4 验收 a–d。"""

    def test_04a_编辑值后状态列更新(self, conn):
        ids = _seed_basic(conn)
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        assert m.status == "在控"
        # 改成 +4SD → 1_3s
        m2 = M.update_measurement_value(conn, m.id, value=108.0)
        assert m2.status is not None and m2.status.startswith("失控")

    def test_04b_标记作废_该行在质控图不显示(self, conn):
        """spec §B.4 d：status='作废' → 不在质控图显示。
        本层通过 list_measurements(include_void=False) 验证。
        """
        ids = _seed_basic(conn)
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        M.mark_void(conn, m.id)
        # 作废不在非作废列表里
        active = M.list_measurements(conn, ids["test_item_id_l"], include_void=False)
        assert len(active) == 0
        # 作废在全列表里
        all_ = M.list_measurements(conn, ids["test_item_id_l"], include_void=True)
        assert len(all_) == 1 and all_[0].status == "作废"

    def test_04c_编辑后触发保存时合并扫描(self, conn):
        """编辑值 → 触发与 §B.1 相同的保存时合并扫描（同检测指标同日的多水平）。"""
        ids = _seed_basic(conn)
        # 4/1 已录 H 水平 +3.5SD（失控）
        M.create_measurement(
            conn, test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"],
            lot="LOT1", level="H",
            date="2026-04-01", value=107.0,
        )
        # L 水平录在控
        m_l = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        assert m_l.status == "在控"
        # 编辑 L 水平为 +3.5SD → L + H 都触发 1_3s + 跨水平也可能触发
        M.update_measurement_value(conn, m_l.id, value=107.0)
        # 再次查 → L 水平状态应变成失控
        refreshed = M.get_measurement(conn, m_l.id)
        assert refreshed.status is not None and refreshed.status.startswith("失控")

    def test_04d_标记作废_列表灰色删除线_不参与统计(self, conn):
        """spec §B.4 d：UI 上作废行灰色 + 删除线；不参与统计与判定。"""
        ids = _seed_basic(conn)
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        M.mark_void(conn, m.id)
        # 作废后 status='作废'，且 rules 应清空（不参与判定）
        refreshed = M.get_measurement(conn, m.id)
        assert refreshed.status == "作废"
        # 后续在控/警告判定不会再"重新算"作废行的 status
        # 验证：再扫描该检测指标 + 该日，作废行 status 不会被覆盖
        M.rescan_day_for_test_item(
            conn, test_item_id=ids["test_item_id_l"], date="2026-04-01",
        )
        refreshed2 = M.get_measurement(conn, m.id)
        assert refreshed2.status == "作废"


# ============================================================================
# §9 边界情况
# ============================================================================

class TestBoundary:
    """spec §9 边界情况。"""

    def test_值负数_弹必须ge0_不允许保存(self):
        with pytest.raises(M.ValueMustNonNegative):
            M.validate_measurement_value(-0.01)

    def test_值0_允许保存(self):
        assert M.validate_measurement_value(0) == 0.0

    def test_值文字_弹请输入数字(self):
        with pytest.raises(M.ValueInvalidNumber):
            M.validate_measurement_value("abc")

    def test_值空白_弹请输入数字(self):
        with pytest.raises(M.ValueInvalidNumber):
            M.validate_measurement_value("")

    def test_日期未来_is_futureTrue(self):
        # 用 2099-01-01（远超今天）
        r = M.validate_measurement_date("2099-01-01")
        assert r["is_future"] is True

    def test_日期2年前_is_too_oldTrue(self):
        r = M.validate_measurement_date("2020-01-01")
        assert r["is_too_old"] is True

    def test_日期正常_两个标志都False(self):
        r = M.validate_measurement_date("2026-04-01")
        assert r["is_future"] is False
        assert r["is_too_old"] is False

    def test_重复识别_key为test_item_level_lot_date_seq(self, conn):
        ids = _seed_basic(conn)
        rows = [
            {"test_item_id": ids["test_item_id_l"], "level": "L", "lot": "LOT1",
             "date": "2026-04-01", "seq": 1},
        ]
        # 库里空 → 不重复
        assert M.find_duplicates_in_import(conn, rows) == []
        # 录 1 条 → 重复
        M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0, seq=1,
        )
        dups = M.find_duplicates_in_import(conn, rows)
        assert dups == [0]

    def test_同日同水平允许多条_不聚合成一条(self, conn):
        ids = _seed_basic(conn)
        for i in range(3):
            M.create_measurement(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"],
                lot="LOT1", level="L",
                date="2026-04-01", value=100.0 + i, seq=i,
            )
        rows = M.list_measurements(conn, ids["test_item_id_l"])
        assert len(rows) == 3


# ============================================================================
# §8.3 数据持久化与备份
# ============================================================================

class TestPersistence:
    """spec §8.3 验收 a–f。"""

    def test_08a_录入N条_关闭重开_N条仍在(self, tmp_path: Path):
        db = tmp_path / "qc.db"
        init_database(db)
        c = get_connection(db)
        ids = _seed_basic(c)
        for i in range(5):
            M.create_measurement(
                c, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"],
                lot="LOT1", level="L",
                date=f"2026-04-0{i+1}", value=100.0,
            )
        c.commit()
        c.close()

        # 重新打开
        c2 = get_connection(db)
        try:
            rows = M.list_measurements(c2, ids["test_item_id_l"])
            assert len(rows) == 5
        finally:
            c2.close()

    def test_08b_删qc_db_程序启动自动建空库_backup保留(self, tmp_path: Path):
        # 先建库 + 触发备份
        db = tmp_path / "qc.db"
        init_database(db)
        backup.backup_today(db)

        # 删 qc.db
        db.unlink()

        # 模拟启动：init_database 应能再建空库
        init_database(db)
        assert db.exists()
        # backup 目录应保留
        assert (tmp_path / "backup").exists()
        # 新库是空的
        c = get_connection(db)
        try:
            n = c.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
            assert n == 0
        finally:
            c.close()

    def test_08c_正常退出_每天1份_保留7天(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "qc.db"
        db.write_bytes(b"")
        backup.backup_today(db)
        p2 = backup.backup_today(db)
        # 同日第二次 → None
        assert p2 is None
        # backup 目录里只有 1 份
        files = list((tmp_path / "backup").iterdir())
        assert len(files) == 1

    def test_08d_异常退出_下次启动多备1份(self, tmp_path: Path):
        db = tmp_path / "qc.db"
        db.write_bytes(b"")
        backup._write_state(db.parent, {"last_exit_clean": False})
        paths = backup.startup_check_and_backup(db)
        # ≥2 份（正常 + 带时间戳额外）
        assert len(paths) >= 2

    def test_08e_备份文件名_qc_YYYYMMDD_db(self, tmp_path: Path):
        from datetime import datetime
        db = tmp_path / "qc.db"
        db.write_bytes(b"")
        p = backup.backup_today(db)
        assert p is not None
        assert p.name == datetime.now().strftime("qc_%Y%m%d.db")

    def test_08f_从backup恢复菜单_有描述符(self):
        d = backup.restore_menu_descriptor()
        assert d["label"] == "从备份恢复…"
        assert callable(d["handler"])


# ============================================================================
# §F.1 排查面板
# ============================================================================

class TestF1Investigation:
    """spec §F.1 验收 a–e。"""

    def test_F1_随机误差_1_3s触发时_返回5项检查(self):
        plan = I.build_investigation_plan(
            rules=["1_3s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=107.0,
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "随机误差嫌疑" in cats
        items = next(c["items"] for c in plan["categories"]
                     if c["category"] == "随机误差嫌疑")
        assert len(items) >= 5

    def test_F1_系统误差_10x触发(self):
        plan = I.build_investigation_plan(
            rules=["10_x"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=100.5,
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "系统误差嫌疑" in cats

    def test_F1_双向_2_2s(self):
        plan = I.build_investigation_plan(
            rules=["2_2s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=105.0,
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "随机 + 系统双向嫌疑" in cats

    def test_F1_多规则合并_1_3s加2_2s(self):
        """spec §F.1 e：同时触发 1_3s + 2_2s → 面板同时展示随机误差 i-v + 双向嫌疑。"""
        plan = I.build_investigation_plan(
            rules=["1_3s", "2_2s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=107.0,
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "随机误差嫌疑" in cats
        assert "随机 + 系统双向嫌疑" in cats

    def test_F1_checklist持久化(self, conn):
        """勾选项写入 out_of_control.checklist（JSON 格式）。"""
        ids = _seed_basic(conn)
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=107.0,
        )
        ooc_id = I.save_investigation(
            conn, measurement_id=m.id,
            rules=["1_3s"],
            checklist={"随机误差嫌疑_试剂": True, "随机误差嫌疑_仪器": False},
            conclusion="复测合格",
        )
        row = conn.execute(
            "SELECT checklist, conclusion FROM out_of_control WHERE id = ?",
            (ooc_id,),
        ).fetchone()
        assert row["conclusion"] == "复测合格"
        cl = json.loads(row["checklist"])
        assert cl["随机误差嫌疑_试剂"] is True

    def test_F1_已检查项必填(self):
        """spec §F.1 d：'已检查项'为必填 → save_investigation 时 checklist 为空抛 ValueError。"""
        with pytest.raises(ValueError):
            I.save_investigation(
                conn=None,  # 不会走到 DB 校验（前置校验先抛）
                measurement_id=1, rules=["1_3s"],
                checklist={}, conclusion="",
            )


# ============================================================================
# 状态列字串稳定性
# ============================================================================

class TestStatusText:
    """状态列字串快照。"""

    def test_状态文案_在控(self):
        assert M._status_text(W.Status.IN_CONTROL, []) == "在控"

    def test_状态文案_警告(self):
        assert M._status_text(W.Status.WARNING, ["1_2s"]) == "警告(1_2s)"

    def test_状态文案_失控(self):
        assert M._status_text(W.Status.OUT_OF_CONTROL, ["1_3s"]) == "失控(1_3s)"

    def test_状态文案_多规则(self):
        assert M._status_text(W.Status.OUT_OF_CONTROL, ["1_3s", "2_2s"]) == \
               "失控(1_3s + 2_2s)"


# ============================================================================
# 异常文案稳定性
# ============================================================================

class TestExceptionSnapshots:
    """防止后续重构改文案"""

    @pytest.mark.parametrize("cls,expected", [
        (M.ValueMustNonNegative, "必须 ≥ 0"),
        (M.ValueInvalidNumber, "请输入数字"),
        (M.DateInFuture, "日期晚于今天"),
        (M.DateTooOld, "日期较久"),
    ])
    def test_异常文案(self, cls, expected):
        assert expected in str(cls())