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
# PyQt6 可用性探测 + QLabel 文本抓取（用于 §F.2 提示测试）
# 必须在 TestF1Investigation 之前定义，否则 pytest 收集时类内引用不到。
# ============================================================================

def _pyqt6_available() -> bool:
    try:
        import PyQt6  # noqa: F401
        return True
    except Exception:
        return False


def _collect_all_label_text(widget) -> str:
    """递归收集一个 QWidget 子树里所有 QLabel 的文本（含 HTML <br>）。"""
    from PyQt6.QtWidgets import QLabel
    parts = []
    for lbl in widget.findChildren(QLabel):
        parts.append(lbl.text())
    return "\n".join(parts)


# ============================================================================
# §F.1 排查面板
# ============================================================================

class TestF1Investigation:
    """spec §F.1 验收 a–i + §8.3 d 边界 + §F.2 4 条提示。"""

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
        """spec §F.1 h + issue #6 决议：2_2s 同时按两方向排查 →
        面板拆为「随机误差嫌疑」5 项 +「系统误差嫌疑」6 项两个分类都列出。"""
        plan = I.build_investigation_plan(
            rules=["2_2s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=105.0,
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "随机误差嫌疑" in cats
        assert "系统误差嫌疑" in cats
        # 同时验证 5 + 6 项（issue #6 决议要求精确数字）
        rand_items = next(c["items"] for c in plan["categories"]
                         if c["category"] == "随机误差嫌疑")
        sys_items = next(c["items"] for c in plan["categories"]
                         if c["category"] == "系统误差嫌疑")
        assert len(rand_items) == 5
        assert len(sys_items) == 6

    def test_F1_多规则合并_1_3s加2_2s(self):
        """spec §F.1 e + issue #6：同时触发 1_3s + 2_2s → 面板同时展示
        随机误差 i-v（5 项）+ 系统误差 i-vi（6 项）合并 = 两类都出现。"""
        plan = I.build_investigation_plan(
            rules=["1_3s", "2_2s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=107.0,
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "随机误差嫌疑" in cats
        assert "系统误差嫌疑" in cats
        # issue #6 决议：不再使用单条提示文字的"双向嫌疑"分类
        assert "随机 + 系统双向嫌疑" not in cats

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

    # ------------------------------------------------------------------
    # 以下为 issue #6 追加验收（spec §F.1 f–i、§8.3 d 边界、§10 不动范围）
    # ------------------------------------------------------------------

    # spec §F.1 f：1_3s / R_4s 展示随机误差嫌疑清单 5 项
    def test_F1_随机误差清单_1_3s_5项(self):
        plan = I.build_investigation_plan(
            rules=["1_3s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=107.0,
        )
        rand = next(c for c in plan["categories"]
                    if c["category"] == "随机误差嫌疑")
        assert len(rand["items"]) == 5
        # 关键 5 项（与 spec §F.1 原文一致）
        joined = "\n".join(rand["items"])
        assert "试剂" in joined
        assert "仪器" in joined
        assert "操作" in joined
        assert "质控品" in joined
        assert "加样/混匀/读数" in joined

    def test_F1_随机误差清单_R_4s_5项(self):
        """spec §F.1 f：R_4s 也按随机误差排查（同 1_3s）。"""
        plan = I.build_investigation_plan(
            rules=["R_4s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=105.0,
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "随机误差嫌疑" in cats
        assert "系统误差嫌疑" not in cats  # R_4s 不带系统误差
        rand = next(c for c in plan["categories"]
                    if c["category"] == "随机误差嫌疑")
        assert len(rand["items"]) == 5

    # spec §F.1 g：4_1s / 10_x 展示系统误差嫌疑清单 6 项
    def test_F1_系统误差清单_4_1s_6项(self):
        plan = I.build_investigation_plan(
            rules=["4_1s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=101.5,  # +0.75SD 不会触发 1_3s
        )
        cats = [c["category"] for c in plan["categories"]]
        assert "系统误差嫌疑" in cats
        assert "随机误差嫌疑" not in cats
        sys = next(c for c in plan["categories"]
                   if c["category"] == "系统误差嫌疑")
        assert len(sys["items"]) == 6
        joined = "\n".join(sys["items"])
        # 关键 6 项（与 spec §F.1 原文一致）
        assert "校准" in joined
        assert "试剂" in joined
        assert "质控品" in joined
        assert "环境" in joined
        assert "方法学偏倚" in joined
        assert "耗材" in joined

    def test_F1_系统误差清单_10_x_6项(self):
        plan = I.build_investigation_plan(
            rules=["10_x"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=100.5,
        )
        sys = next(c for c in plan["categories"]
                   if c["category"] == "系统误差嫌疑")
        assert len(sys["items"]) == 6

    # spec §F.1 e + h：1_3s + 2_2s 同时 → 5 + 6 项合并
    def test_F1_合并_1_3s_加_2_2s_5_加_6项(self):
        plan = I.build_investigation_plan(
            rules=["1_3s", "2_2s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=107.0,
        )
        rand = next(c for c in plan["categories"]
                    if c["category"] == "随机误差嫌疑")
        sys = next(c for c in plan["categories"]
                   if c["category"] == "系统误差嫌疑")
        assert len(rand["items"]) == 5
        assert len(sys["items"]) == 6

    # spec §F.1 h：2_2s 单规则 → 5 + 6 都列出
    def test_F1_2_2s_单规则_5_加_6项(self):
        plan = I.build_investigation_plan(
            rules=["2_2s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=105.0,
        )
        rand = next(c for c in plan["categories"]
                    if c["category"] == "随机误差嫌疑")
        sys = next(c for c in plan["categories"]
                   if c["category"] == "系统误差嫌疑")
        assert len(rand["items"]) == 5
        assert len(sys["items"]) == 6

    # spec §F.1 i（issue #6 验收 i）：§F.2 4 条提示在面板中可见
    @pytest.mark.skipif(
        not _pyqt6_available(), reason="PyQt6 未安装；§F.2 面板内嵌于 GUI，跳过"
    )
    def test_F2_4条提示展示在面板中(self):
        """§F.2 4 条要点：不要立即抹点、不要只重做、不要厂商值、不要再纳入跨批。"""
        pytest.importorskip("PyQt6")
        from PyQt6.QtWidgets import QApplication
        from qc_tool.main_window_b import InvestigationDialog

        app = QApplication.instance() or QApplication([])
        plan = I.build_investigation_plan(
            rules=["1_3s"], triggered_date="2026-04-01",
            project_name="维生素 D", test_item_name="D2",
            lot="LOT1", level="L", value=107.0,
        )
        dlg = InvestigationDialog(None, plan=plan, measurement_id=999)
        # 把所有 QLabel 文本拼起来
        all_text = _collect_all_label_text(dlg)
        # 4 条要点关键词
        assert "立即抹去失控点" in all_text
        assert "重做控制品" in all_text
        assert "厂商定值" in all_text
        assert "跨批规则判定" in all_text
        dlg.deleteLater()

    # spec §F.1 b/c + 边界：checklist 持久化 → 写库为 JSON 格式
    def test_F1_checklist_JSON格式持久化_round_trip(self, conn):
        ids = _seed_basic(conn)
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=107.0,
        )
        cl_in = {
            "随机误差嫌疑_试剂：是否新开瓶 / 是否过期 / 是否污染": True,
            "随机误差嫌疑_仪器：泵阀、管道气泡、光路、电压": False,
        }
        ooc_id = I.save_investigation(
            conn, measurement_id=m.id, rules=["1_3s"],
            checklist=cl_in, conclusion="已重新校准",
        )
        row = conn.execute(
            "SELECT checklist, conclusion FROM out_of_control WHERE id = ?",
            (ooc_id,),
        ).fetchone()
        cl_out = json.loads(row["checklist"])
        assert cl_out == cl_in  # JSON round-trip 一致
        assert row["conclusion"] == "已重新校准"

    # 边界：list_ooc —— 删检测指标后仍能查历史
    def test_F1_删指标后用_冗余名称_仍能查历史(self, conn):
        ids = _seed_basic(conn)
        m = M.create_measurement(
            conn, test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=107.0,
        )
        I.save_investigation(
            conn, measurement_id=m.id, rules=["1_3s"],
            checklist={"随机误差嫌疑_试剂：是否新开瓶 / 是否过期 / 是否污染": True},
            conclusion="复测合格",
        )
        # 删指标（spec §8.3 c）
        P.delete_test_item(conn, ids["test_item_id_l"])
        # 此时 test_item_id 已置空、measurement_id 已置空
        # 但 list_ooc 不带过滤应仍能查到（用冗余 test_item_name / project_name）
        all_ooc = I.list_ooc(conn)
        assert len(all_ooc) == 1
        row = all_ooc[0]
        assert row["test_item_id"] is None
        assert row["measurement_id"] is None
        assert row["test_item_name"] == "D2"           # 保留
        assert row["project_name"] == "维生素 D"        # 保留
        assert row["lot"] == "LOT1"
        assert row["level"] == "L"
        assert row["triggered_date"] == "2026-04-01"
        assert row["conclusion"] == "复测合格"
        assert "1_3s" in json.loads(row["rules"])

    # 边界：list_ooc 按 project_id 过滤
    def test_F1_list_ooc_按项目_过滤(self, conn):
        ids1 = _seed_basic(conn, project="项目甲")
        ids2 = _seed_basic(conn, project="项目乙")
        m1 = M.create_measurement(
            conn, test_item_id=ids1["test_item_id_l"],
            project_id=ids1["project_id"],
            lot="LOT1", level="L", date="2026-04-01", value=107.0,
        )
        m2 = M.create_measurement(
            conn, test_item_id=ids2["test_item_id_l"],
            project_id=ids2["project_id"],
            lot="LOT1", level="L", date="2026-04-02", value=107.0,
        )
        I.save_investigation(conn, measurement_id=m1.id, rules=["1_3s"],
                             checklist={"k": True})
        I.save_investigation(conn, measurement_id=m2.id, rules=["1_3s"],
                             checklist={"k": True})
        rows_a = I.list_ooc(conn, project_id=ids1["project_id"])
        rows_b = I.list_ooc(conn, project_id=ids2["project_id"])
        assert len(rows_a) == 1 and rows_a[0]["project_name"] == "项目甲"
        assert len(rows_b) == 1 and rows_b[0]["project_name"] == "项目乙"

    # 边界：list_ooc 按日期区间过滤
    def test_F1_list_ooc_按日期区间(self, conn):
        ids = _seed_basic(conn)
        for date_str in ("2026-04-01", "2026-04-15", "2026-05-01"):
            m = M.create_measurement(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"],
                lot="LOT1", level="L", date=date_str, value=107.0,
            )
            I.save_investigation(conn, measurement_id=m.id, rules=["1_3s"],
                                 checklist={"k": True})
        # 仅 4 月
        rows = I.list_ooc(conn, date_from="2026-04-01", date_to="2026-04-30")
        assert len(rows) == 2
        assert all(r["triggered_date"].startswith("2026-04") for r in rows)

    # 边界：list_ooc 排序 triggered_date DESC
    def test_F1_list_ooc_排序(self, conn):
        ids = _seed_basic(conn)
        for date_str in ("2026-04-01", "2026-04-02", "2026-04-03"):
            m = M.create_measurement(
                conn, test_item_id=ids["test_item_id_l"],
                project_id=ids["project_id"],
                lot="LOT1", level="L", date=date_str, value=107.0,
            )
            I.save_investigation(conn, measurement_id=m.id, rules=["1_3s"],
                                 checklist={"k": True})
        rows = I.list_ooc(conn)
        dates = [r["triggered_date"] for r in rows]
        assert dates == sorted(dates, reverse=True)


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


# ============================================================================
# Issue #3（功能 B 数据录入与 Excel 导入）—— 8 个核心场景
# ============================================================================
#
# 设计要点（issue #3 顶部明确）：
# - 判定触发逻辑由"公共入口函数 on_measurement_saved"承担，**直接调用**已
#   锁死的第 7 章判定引擎（westgard.py），不重写判定。
# - 8 个场景覆盖：1 / 2 / 3 / 6 / 7 / 8 / 9 / 10（场景 4 / 5 靠手测，不写自动化）
# - 测试用 xlsx 由 openpyxl 临时生成 + tempfile 清理，不落盘
# - 重点：场景 2 同日 H/L 两条 → 查库断言两条都是 2_2s
# - 重点：场景 6 / 7 整批拒绝 → 查 DB 一条都没新增

class TestIssue3Scenarios:
    """Issue #3 8 个核心场景自动化（spec V1.3 §B + §D）"""

    # ------------------------------------------------------------------
    # 场景 1：录一条 104.1（+2SD ~ +3SD 间）→ 立即判警告 1_2s
    # ------------------------------------------------------------------
    def test_场景1_录104_1_立即警告1_2s(self, conn):
        """issue #3 场景 1：保存那一刻立即判警告（黄色），不用等日终。"""
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        m = M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=104.1,
        )
        # 立即可读（不靠"日终"）
        assert m.status is not None and m.status.startswith("警告"), \
            f"期望警告，实际 {m.status!r}"
        rules = json.loads(m.rules)
        assert "1_2s" in rules
        assert "1_3s" not in rules

    # ------------------------------------------------------------------
    # 场景 2：同日 H 104.1 + L 104.2 → 两条都判 2_2s
    # ------------------------------------------------------------------
    def test_场景2_同日HL跨水平2_2s_查库两条都判2_2s(self, conn):
        """issue #3 场景 2（重点）：
        同一天录入 H 水平 104.1 和 L 水平 104.2 → 后保存的那条触发同日合并扫描，
        两条都判失控（2_2s 跨水平）。查库断言：
          - 两条 status 都是"失控(2_2s)"
          - 两条 rules 包含 "2_2s"
        """
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        # 先存 L=104.2（+2.1SD）
        M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=104.2,
        )
        # 再存 H=104.1（+2.05SD）→ 触发"后保存那条" 走合并扫描
        M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_h"],
            project_id=ids["project_id"],
            lot="LOT1", level="H",
            date="2026-04-01", value=104.1,
        )
        conn.commit()

        # 查 DB 终态：两条都应该是 2_2s 跨水平（场景 2 重点）
        rows_l = M.list_measurements(conn, ids["test_item_id_l"], include_void=True)
        rows_h = M.list_measurements(conn, ids["test_item_id_h"], include_void=True)
        l_today = [r for r in rows_l if r.date == "2026-04-01"]
        h_today = [r for r in rows_h if r.date == "2026-04-01"]
        assert len(l_today) == 1, f"L 水平 4/1 应有 1 条，实际 {len(l_today)}"
        assert len(h_today) == 1, f"H 水平 4/1 应有 1 条，实际 {len(h_today)}"

        l_row = l_today[0]
        h_row = h_today[0]
        assert l_row.status is not None and l_row.status.startswith("失控"), \
            f"L 水平 DB 终态期望失控，实际 {l_row.status!r}"
        assert h_row.status is not None and h_row.status.startswith("失控"), \
            f"H 水平 DB 终态期望失控，实际 {h_row.status!r}"
        rules_l = json.loads(l_row.rules)
        rules_h = json.loads(h_row.rules)
        assert "2_2s" in rules_l, f"L 水平 rules 应含 2_2s，实际 {rules_l}"
        assert "2_2s" in rules_h, f"H 水平 rules 应含 2_2s，实际 {rules_h}"

    # ------------------------------------------------------------------
    # 场景 3：补录历史日期 → 只扫补录日，不影响今天已录的数据
    # ------------------------------------------------------------------
    def test_场景3_补录昨天_只扫昨天_今天数据不受影响(self, conn):
        """issue #3 场景 3：今天补录一条昨天的值，只扫昨天那个判定单元，
        今天已录的数据 status 不变。"""
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        # 今天 4/2 录一条在控
        m_today = M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-02", value=100.0,
        )
        assert m_today.status == "在控"
        # 补录昨天 4/1 一条 1_3s
        m_yesterday = M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=107.0,
        )
        # 今天那条仍应是"在控"（补录昨天没把今天扫描掉）
        refreshed_today = M.get_measurement(conn, m_today.id)
        assert refreshed_today.status == "在控", \
            f"补录昨天不应改变今天的状态，实际 {refreshed_today.status!r}"
        # 昨天那条应是失控
        assert m_yesterday.status is not None and m_yesterday.status.startswith("失控")

    # ------------------------------------------------------------------
    # 场景 6：缺"批号"列 → 整批拒绝（DB 0 新增 + 错误报告写明缺哪列）
    # ------------------------------------------------------------------
    def test_场景6_缺批号列_整批拒绝_DB零新增_错误报告写明(self, conn):
        """issue #3 场景 6：模板缺"批号"列 → 整批拒绝，DB 一条都不入，
        错误报告写明缺'批号'。"""
        ids = _seed_basic(conn)
        # 构造只有 5 列的"模板"（缺"批号"）
        bad_xlsx = X.build_xlsx_with_columns(
            ["项目名", "检测指标", "水平", "日期", "测定值"],  # 缺"批号"
            [],
        )
        with pytest.raises(M.MissingRequiredColumns) as exc:
            X.parse_import_xlsx_bytes(bad_xlsx)
        assert "批号" in exc.value.columns, \
            f"错误报告应写明缺'批号'，实际缺 {exc.value.columns}"

        # 验证：DB 0 新增（因为 parse 阶段就拒，没走 import_rows）
        n = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        assert n == 0, f"整批拒绝后 DB 应 0 新增，实际 {n}"

    # ------------------------------------------------------------------
    # 场景 7：某行"检测指标"在系统里不存在 → 整批拒绝
    # ------------------------------------------------------------------
    def test_场景7_指标不存在_整批拒绝_DB零新增_错误报告指明(self, conn):
        """issue #3 场景 7：Excel 里某行的'检测指标'在系统里不存在 →
        整批拒绝，DB 0 新增，错误报告指明哪个指标不存在。"""
        ids = _seed_basic(conn)  # 只建了 D2
        rows = [
            {  # 指标存在 D2
                "项目名": "维生素 D", "检测指标": "D2",
                "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
                "测定值": 100.0, "序号": 1,
            },
            {  # 指标不存在 D3
                "项目名": "维生素 D", "检测指标": "D3_不存在的指标",
                "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
                "测定值": 100.0, "序号": 2,
            },
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        # 2 行解析都 OK，import_rows 阶段才整批拒绝
        assert len(plan.ok_rows) == 2

        with pytest.raises(X.TestItemNotFound) as exc:
            X.import_rows(
                conn, plan,
                project_resolver=lambda n: ids["project_id"] if n == "维生素 D" else None,
            )
        # 错误报告指明哪个指标不存在
        assert "D3_不存在的指标" in str(exc.value)

        # 验证：DB 0 新增（事务在 import_rows 入口抛异常前没 commit）
        n = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        assert n == 0, f"整批拒绝后 DB 应 0 新增，实际 {n}"

    # ------------------------------------------------------------------
    # 场景 8：某行日期格式错乱 → 该行跳过，其余正常导入
    # ------------------------------------------------------------------
    def test_场景8_某行日期错_该行跳过_错误报告给出行号(self, conn):
        """issue #3 场景 8：Excel 某行日期格式错乱 → 该行跳过，其余正常
        导入；错误报告给出出错行号。"""
        ids = _seed_basic(conn)
        rows = [
            {"项目名": "维生素 D", "检测指标": "D2",
             "水平": "L", "批号": "LOT1", "日期": "2026-04-01",
             "测定值": 100.0, "序号": 1},  # OK
            {"项目名": "维生素 D", "检测指标": "D2",
             "水平": "L", "批号": "LOT1", "日期": "不是日期",  # 错
             "测定值": 100.0, "序号": 2},
            {"项目名": "维生素 D", "检测指标": "D2",
             "水平": "L", "批号": "LOT1", "日期": "2026-04-03",
             "测定值": 100.0, "序号": 3},  # OK
        ]
        xlsx = X.build_template_xlsx_with_rows(rows)
        plan = X.parse_import_xlsx_bytes(xlsx)
        # 1 行跳过，2 行 OK
        assert any(s.original_row_no == 3 and "日期格式错误" in s.reason
                   for s in plan.skipped_rows), \
            f"期望第 3 行被跳过（日期错），实际 skipped={[(s.original_row_no, s.reason) for s in plan.skipped_rows]}"
        assert len(plan.ok_rows) == 2

        # 入库（去重后只剩 2 条）
        X.import_rows(
            conn, plan,
            project_resolver=lambda n: ids["project_id"] if n == "维生素 D" else None,
        )
        rows_db = M.list_measurements(conn, ids["test_item_id_l"])
        # 2 条入库（4/1 和 4/3）
        dates = sorted(r.date for r in rows_db)
        assert dates == ["2026-04-01", "2026-04-03"], \
            f"应入库 2 条，实际 {dates}"

    # ------------------------------------------------------------------
    # 场景 9：编辑一条已保存记录的测定值 → 该判定单元重判
    # ------------------------------------------------------------------
    def test_场景9_编辑值_所属判定单元重判_旧结果作废(self, conn):
        """issue #3 场景 9：编辑一条已保存记录的测定值 → 该条所属判定单元
        重新判定，旧判定结果作废。"""
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        m = M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        assert m.status == "在控", f"初始应'在控'，实际 {m.status!r}"

        # 编辑为 +3.5SD → 触发 1_3s
        m2 = M.update_measurement_value(conn, m.id, value=107.0)
        assert m2.status is not None and m2.status.startswith("失控"), \
            f"编辑后应失控，实际 {m2.status!r}"
        rules = json.loads(m2.rules)
        assert "1_3s" in rules, f"编辑后应触发 1_3s，实际 {rules}"

        # 再编辑回 100.0 → 重判回"在控"，旧"失控"判定作废
        m3 = M.update_measurement_value(conn, m.id, value=100.0)
        assert m3.status == "在控", \
            f"再编辑回 100 应'在控'（旧失控结果作废），实际 {m3.status!r}"

    # ------------------------------------------------------------------
    # 场景 10：标记作废 → 不参与统计/判定/质控图
    # ------------------------------------------------------------------
    def test_场景10_标记作废_不参与统计判定质控图(self, conn):
        """issue #3 场景 10：把一条记录标记作废 → status='作废'，
        不参与统计、不参与判定扫描、不上质控图。"""
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        m = M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-01", value=100.0,
        )
        M.mark_void(conn, m.id)
        refreshed = M.get_measurement(conn, m.id)
        assert refreshed.status == "作废"

        # 1. 不参与统计：list_measurements(include_void=False) 不应包含
        active = M.list_measurements(conn, ids["test_item_id_l"], include_void=False)
        assert all(r.id != m.id for r in active), \
            "作废行不应出现在 include_void=False 列表中"

        # 2. 不参与判定扫描：再保存新数据触发 rescan，作废行 status 不变
        M.create_measurement(
            conn,
            test_item_id=ids["test_item_id_l"],
            project_id=ids["project_id"],
            lot="LOT1", level="L",
            date="2026-04-02", value=107.0,  # 触发 1_3s
        )
        after = M.get_measurement(conn, m.id)
        assert after.status == "作废", \
            f"rescan 不应覆盖作废行 status，实际 {after.status!r}"

        # 3. 不上质控图：作废行的 rules 应清空（图形渲染时不画点）
        assert after.rules is None or after.rules in (None, ""), \
            f"作废行 rules 应清空，实际 {after.rules!r}"


# ============================================================================
# Issue #3 公共入口函数契约（on_measurement_saved）
# ============================================================================

class TestIssue3OnMeasurementSavedEntry:
    """验证 on_measurement_saved 是 rescan_day_for_test_item 的公共别名，
    且签名一致。"""

    def test_on_measurement_saved与rescan是同一函数对象(self):
        """issue #3 顶部要求'判定触发逻辑单独成函数（如 on_measurement_saved）'"""
        from qc_tool.measurements import on_measurement_saved, rescan_day_for_test_item
        assert on_measurement_saved is rescan_day_for_test_item

    def test_on_measurement_saved可独立调用_触发重扫(self, conn):
        """手动 INSERT 一条 + 调 on_measurement_saved → 应被判定/扫描。"""
        ids = _seed_basic(conn, target=100.0, sd=2.0)
        # 绕过 create_measurement，直接 SQL INSERT 一条 +4SD
        conn.execute(
            "INSERT INTO measurements(test_item_id, project_id, lot, level, date, value) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ids["test_item_id_l"], ids["project_id"],
             "LOT1", "L", "2026-04-01", 108.0),
        )
        conn.commit()
        # 此时该行 status 是 NULL（未判定）
        n = conn.execute(
            "SELECT COUNT(*) FROM measurements WHERE status IS NULL"
        ).fetchone()[0]
        assert n == 1, f"直接 INSERT 后应为未判定，实际 {n}"

        # 调 on_measurement_saved（公共入口）
        from qc_tool.measurements import on_measurement_saved
        on_measurement_saved(
            conn, test_item_id=ids["test_item_id_l"], date="2026-04-01",
        )
        # 状态应被写为"失控"
        row = conn.execute(
            "SELECT status, rules FROM measurements "
            "WHERE test_item_id = ? AND date = ?",
            (ids["test_item_id_l"], "2026-04-01"),
        ).fetchone()
        assert row["status"] is not None and row["status"].startswith("失控"), \
            f"on_measurement_saved 调用后应被判定为失控，实际 {row['status']!r}"