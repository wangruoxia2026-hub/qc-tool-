"""
功能 A（spec §A.1–A.5 + §8.3）业务层验收测试

按 issue 验收清单逐条覆盖 + 关键边界 + spec §8.3 测试。

依赖：db.py（spec §8.1 schema 原样建表）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qc_tool.db import init_database
from qc_tool.projects import (
    Project,
    TestItem,
    DeleteSummary,
    ProjectNameEmpty,
    ProjectNameExists,
    LabNameEmpty,
    InvalidNumberFormat,
    NonPositiveNumber,
    LotExists,
    create_project,
    get_project,
    list_projects,
    update_project,
    delete_project,
    create_test_item,
    get_test_item,
    list_test_items,
    list_lots,
    update_test_item,
    delete_test_item,
    add_lot,
    get_lab_name,
    set_lab_name,
    is_first_project_pending,
    get_connection,
)


# ============================================================================
# Fixture：每个测试用全新空库，不互相污染；保证 PRAGMA foreign_keys=ON。
# ============================================================================

@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "qc.db"
    init_database(db)
    connection = get_connection(db)
    # lab_settings 占位行
    connection.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, NULL)")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


# ============================================================================
# A.1 新建检验项目
# ============================================================================

class TestA1CreateProject:
    """spec §A.1 验收"""

    def test_验收a_填齐必填项列表立刻出现(self, conn):
        p = create_project(conn, name="维生素 D", short="Vit-D", unit="ng/mL", manufacturer="Roche")
        assert isinstance(p, Project)
        assert p.id > 0
        assert p.name == "维生素 D"

        listed = list_projects(conn)
        assert len(listed) == 1
        assert listed[0].id == p.id

    def test_验收b_项目名称重复(self, conn):
        create_project(conn, name="维生素 D")
        with pytest.raises(ProjectNameExists) as exc_info:
            create_project(conn, name="维生素 D")
        assert "已存在同名项目" in str(exc_info.value)

    def test_验收c_项目名称为空(self, conn):
        with pytest.raises(ProjectNameEmpty) as exc_info:
            create_project(conn, name="")
        assert "项目名称不能为空" in str(exc_info.value)

    def test_验收c_项目名称全是空格(self, conn):
        with pytest.raises(ProjectNameEmpty):
            create_project(conn, name="   ")

    def test_验收c_项目名称为None(self, conn):
        with pytest.raises(ProjectNameEmpty):
            create_project(conn, name=None)

    def test_验收d_lab_name不在项目级(self, conn):
        """§A.1 d：实验室名称从项目级移除。
        项目级只能存 name/short/unit/manufacturer 字段，不存在 lab_name。
        """
        p = create_project(conn, name="维生素 D")
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (p.id,)).fetchone()
        cols = set(row.keys())
        assert "lab_name" not in cols


# ============================================================================
# A.2 新建检测指标
# ============================================================================

class TestA2CreateTestItem:
    """spec §A.2 验收"""

    def _setup_project(self, conn) -> int:
        p = create_project(conn, name="维生素 D")
        return p.id

    def test_验收a_同一项目下多个检测指标(self, conn):
        pid = self._setup_project(conn)
        create_test_item(
            conn, pid, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0
        )
        create_test_item(
            conn, pid, name="D3", lot="LOT1", level="L", target=20.0, sd=2.0
        )
        items = list_test_items(conn, pid)
        names = sorted(i.name for i in items)
        assert names == ["D2", "D3"]

    def test_验收b_不同项目可同名(self, conn):
        p1 = create_project(conn, name="维生素 D")
        p2 = create_project(conn, name="维生素 K")
        create_test_item(conn, p1.id, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0)
        # 跨项目同名不冲突
        create_test_item(conn, p2.id, name="D2", lot="LOT1", level="L", target=15.0, sd=1.5)

        items1 = list_test_items(conn, p1.id, name="D2")
        items2 = list_test_items(conn, p2.id, name="D2")
        assert len(items1) == 1
        assert len(items2) == 1
        assert items1[0].target == 10.0
        assert items2[0].target == 15.0

    def test_验收c_每个指标独立配2水平(self, conn):
        pid = self._setup_project(conn)
        create_test_item(conn, pid, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0)
        create_test_item(conn, pid, name="D2", lot="LOT1", level="H", target=30.0, sd=2.0)

        items = list_test_items(conn, pid)
        levels = sorted(i.level for i in items)
        assert levels == ["H", "L"]

    def test_验收d_质控图元信息可配置_本期跳过(self):
        """spec §A.2 d：图表配置元信息勾选。GUI 配置属功能 E/E.1，本期
        只在 tests 留 TODO 标记，UI 阶段在功能 E 一并实现（issue 不在本期）。"""
        pytest.skip("本期跳过 §A.2 d（UI 项），由功能 E 负责")

    def test_边界_靶值非正(self, conn):
        pid = self._setup_project(conn)
        with pytest.raises(NonPositiveNumber) as exc_info:
            create_test_item(conn, pid, name="D2", lot="LOT1", level="L", target=0, sd=1.0)
        assert "靶值" in str(exc_info.value)

    def test_边界_SD_非正(self, conn):
        pid = self._setup_project(conn)
        with pytest.raises(NonPositiveNumber) as exc_info:
            create_test_item(conn, pid, name="D2", lot="LOT1", level="L", target=10.0, sd=-1.0)
        assert "SD" in str(exc_info.value)

    def test_边界_靶值非数字(self, conn):
        pid = self._setup_project(conn)
        with pytest.raises(InvalidNumberFormat) as exc_info:
            create_test_item(conn, pid, name="D2", lot="LOT1", level="L", target="abc", sd=1.0)
        assert "靶值" in str(exc_info.value)
        assert "请输入数字" in str(exc_info.value)

    def test_边界_SD_非数字(self, conn):
        pid = self._setup_project(conn)
        with pytest.raises(InvalidNumberFormat) as exc_info:
            create_test_item(conn, pid, name="D2", lot="LOT1", level="L", target=10.0, sd="xyz")
        assert "SD" in str(exc_info.value)
        assert "请输入数字" in str(exc_info.value)

    def test_边界_已有批号引导走添加新批号(self, conn):
        """spec §A.4 / O3 / issue：同 (project, name, lot, level) 已存在 → 禁止覆盖，
        业务层抛 LotExists，引导走 add_lot 路径"""
        pid = self._setup_project(conn)
        create_test_item(conn, pid, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0)
        with pytest.raises(LotExists) as exc_info:
            create_test_item(conn, pid, name="D2", lot="LOT1", level="L", target=11.0, sd=1.0)
        assert "LOT1" in str(exc_info.value)

    def test_边界_level非法(self, conn):
        pid = self._setup_project(conn)
        with pytest.raises(ValueError):
            create_test_item(conn, pid, name="D2", lot="LOT1", level="X", target=10.0, sd=1.0)

    def test_边界_项目不存在(self, conn):
        with pytest.raises(LookupError):
            create_test_item(conn, 9999, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0)


# ============================================================================
# A.3 编辑 / 删除
# ============================================================================

class TestA3EditDelete:
    """spec §A.3.1 / §A.3.2 + §8.3 c"""

    def _setup(self, conn):
        p = create_project(conn, name="维生素 D")
        t = create_test_item(
            conn, p.id, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0
        )
        return p, t

    def test_验收a_改项目名_视图刷新(self, conn):
        p, _ = self._setup(conn)
        updated = update_project(conn, p.id, name="Vit D Total")
        assert updated.name == "Vit D Total"

        re = get_project(conn, p.id)
        assert re.name == "Vit D Total"

    def test_验收a_改项目名为重复_抛同名异常(self, conn):
        create_project(conn, name="维生素 K")
        p, _ = self._setup(conn)  # 维生素 D
        with pytest.raises(ProjectNameExists):
            update_project(conn, p.id, name="维生素 K")

    def test_验收c_改靶值_不影响历史(self, conn):
        """改 target 后 row 上 target 改变；Westgard 判定以行内当前值为准，
        所以历史判定结果不重算（spec §A.3.2 a / §D.5）—— 本层验证 SQL 层字段更新。
        Westgard 不重算的端到端验证由 westgard.py 测试覆盖（test_westgard.py 已
        验证 evaluate 时传 target/sd 是参数式行为，与 storage 解耦）。
        """
        _, ti = self._setup(conn)
        updated = update_test_item(conn, ti.id, target=12.5)
        assert updated.target == 12.5

        re = get_test_item(conn, ti.id)
        assert re.target == 12.5

    def test_边界_改靶值小于0_抛(self, conn):
        _, ti = self._setup(conn)
        with pytest.raises(NonPositiveNumber):
            update_test_item(conn, ti.id, target=0)

    def test_边界_改SD非数字_抛(self, conn):
        _, ti = self._setup(conn)
        with pytest.raises(InvalidNumberFormat):
            update_test_item(conn, ti.id, sd="not a number")

    # ----- 删除项目 -----

    def test_验收b_删项目弹统计(self, conn):
        """spec §A.3.1 / §8.3 c：删项目时返回的 DeleteSummary
        应包含 n_test_items / n_measurements / n_out_of_control 三项
        —— 上层 GUI 用此数据弹'将删除 N 条 measurements'类二次确认。"""
        p, t = self._setup(conn)
        # 加一条 measurement 用于测试统计
        conn.execute(
            "INSERT INTO measurements("
            "  test_item_id, project_id, lot, level, date, value"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (t.id, p.id, "LOT1", "L", "2026-04-01", 12.0),
        )
        # 加一条 out_of_control
        conn.execute(
            "INSERT INTO out_of_control("
            "  measurement_id, test_item_id, project_id,"
            "  lot, level, triggered_date, test_item_name, project_name,"
            "  rules, conclusion"
            ") VALUES (1, ?, ?, 'LOT1', 'L', '2026-04-01', 'D2', '维生素 D',"
            "  '[\"1_3s\"]', '复测合格')",
            (t.id, p.id),
        )
        conn.commit()

        summary = delete_project(conn, p.id)
        assert isinstance(summary, DeleteSummary)
        assert summary.n_test_items == 1
        assert summary.n_measurements == 1
        # out_of_control 按 §8.3 c 必须保留；统计只用来告知用户
        assert summary.n_out_of_control == 1

    def test_验收b_删项目实测CASCADE与保留(self, conn):
        """spec §8.3 c 行为级验证"""
        p, t = self._setup(conn)
        conn.execute(
            "INSERT INTO measurements("
            "  test_item_id, project_id, lot, level, date, value"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (t.id, p.id, "LOT1", "L", "2026-04-01", 12.0),
        )
        conn.execute(
            "INSERT INTO out_of_control("
            "  measurement_id, test_item_id, project_id,"
            "  lot, level, triggered_date, test_item_name, project_name,"
            "  rules, conclusion"
            ") VALUES (1, ?, ?, 'LOT1', 'L', '2026-04-01', 'D2', '维生素 D',"
            "  '[\"1_3s\"]', '复测合格')",
            (t.id, p.id),
        )
        conn.commit()

        delete_project(conn, p.id)

        # measurements CASCADE 删除
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
        # projects 已删
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        # out_of_control 保留；measurement_id 因 FK 置空
        row = conn.execute(
            "SELECT measurement_id, project_id, test_item_id,"
            "       lot, level, triggered_date, rules, conclusion,"
            "       test_item_name, project_name "
            "FROM out_of_control"
        ).fetchone()
        assert row[0] is None  # measurement_id SET NULL
        assert row[3] == "LOT1"
        assert row[4] == "L"
        assert row[5] == "2026-04-01"
        assert row[8] == "D2"
        assert row[9] == "维生素 D"

    # ----- 删除检测指标 -----

    def test_验收d_删指标弹统计(self, conn):
        _, ti = self._setup(conn)
        conn.execute(
            "INSERT INTO measurements(test_item_id, project_id, lot, level, date, value) "
            "VALUES (?, 1, 'LOT1', 'L', '2026-04-01', 12.0)",
            (ti.id,),
        )
        conn.commit()

        summary = delete_test_item(conn, ti.id)
        assert summary.n_measurements == 1

    def test_验收e_删指标CASCADE_并保留ooc(self, conn):
        """spec §A.3.2 e + §8.3 c"""
        _, ti = self._setup(conn)
        conn.execute(
            "INSERT INTO measurements(test_item_id, project_id, lot, level, date, value) "
            "VALUES (?, 1, 'LOT1', 'L', '2026-04-01', 12.0)",
            (ti.id,),
        )
        conn.execute(
            "INSERT INTO out_of_control("
            "  measurement_id, test_item_id, project_id,"
            "  lot, level, triggered_date, test_item_name, project_name,"
            "  rules, conclusion"
            ") VALUES (1, ?, 1, 'LOT1', 'L', '2026-04-01', 'D2', '维生素 D',"
            "  '[\"1_3s\"]', '复测合格')",
            (ti.id,),
        )
        conn.commit()

        delete_test_item(conn, ti.id)

        # measurements CASCADE
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
        # out_of_control 保留
        row = conn.execute(
            "SELECT measurement_id, test_item_id,"
            "       lot, level, triggered_date, rules, conclusion,"
            "       test_item_name "
            "FROM out_of_control"
        ).fetchone()
        assert row[0] is None   # measurement_id SET NULL
        assert row[2] == "LOT1"
        assert row[3] == "L"
        assert row[4] == "2026-04-01"
        assert row[5] == '["1_3s"]'
        assert row[6] == "复测合格"
        assert row[7] == "D2"   # test_item_name 保留

    def test_删指标显示已删除(self, conn):
        """spec §8.3 d：test_item_id IS NULL → 前端显示'检测指标已删除'。
        本层用 list_test_items 验证：item 不存在时返回 None。"""
        _, ti = self._setup(conn)
        conn.execute(
            "INSERT INTO out_of_control("
            "  test_item_id, project_id, lot, level, triggered_date,"
            "  test_item_name, project_name, rules, conclusion"
            ") VALUES (?, 1, 'LOT1', 'L', '2026-04-01', 'D2', '维生素 D',"
            "  '[\"1_3s\"]', '复测合格')",
            (ti.id,),
        )
        conn.commit()

        delete_test_item(conn, ti.id)

        # 上层界面按 test_item_id IS NULL 处理 → get_test_item(None) 没有意义
        # 验证 out_of_control 行的 test_item_name 仍可被前端用于显示
        row = conn.execute(
            "SELECT test_item_id, test_item_name FROM out_of_control"
        ).fetchone()
        # test_item_id 没被自动置空（schema 没 FK），但 item 已不存在
        # 上层查 get_test_item(row[0]) 会得到 None → 显示'已删除'并以 name 兜底
        assert row[1] == "D2"
        assert get_test_item(conn, row[0]) is None


# ============================================================================
# A.4 添加新批号
# ============================================================================

class TestA4AddLot:
    """spec §A.4"""

    def test_验收a_新批号在下拉可见(self, conn):
        p = create_project(conn, name="维生素 D")
        ti1 = create_test_item(
            conn, p.id, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0
        )

        # 添加新批号 —— 通过 add_lot
        ti2 = add_lot(
            conn, ti1.id,
            new_lot="LOT2",
            target=11.0,
            sd=1.1,
        )

        lots = list_lots(conn, p.id, "D2")
        assert sorted(lots) == ["LOT1", "LOT2"]

    def test_验收a_新批号数据隔离(self, conn):
        """spec §A.4 b：新批号的规则历史按 §D.3 对齐维度清零
        （westgard 本身按四维分组；本层验证新批号是新行，行为由 westgard 测试覆盖）"""
        p = create_project(conn, name="维生素 D")
        ti1 = create_test_item(
            conn, p.id, name="D2", lot="LOT1", level="L", target=10.0, sd=1.0
        )
        ti2 = add_lot(conn, ti1.id, new_lot="LOT2", target=10.0, sd=1.0)

        assert ti1.lot == "LOT1"
        assert ti2.lot == "LOT2"
        assert ti1.id != ti2.id


# ============================================================================
# A.5 全局设置（实验室名称）
# ============================================================================

class TestA5LabSettings:
    """spec §A.5"""

    def test_首次空库_lab_name为None(self, conn):
        assert get_lab_name(conn) is None

    def test_验收a_首次未设_返回pending(self, conn):
        """spec §A.5 a：首次创建项目时未填写 → 系统设置页自动弹出"""
        assert is_first_project_pending(conn) is True

    def test_验收a_设了lab_name后pending消失(self, conn):
        set_lab_name(conn, "检验科")
        assert is_first_project_pending(conn) is False

    def test_验收a_已有项目但lab_name仍空_pending仍True(self, conn):
        """spec §A.5 a 边界：曾创建过项目但未填 lab_name 也算 pending
        （只要 lab_name 空且任何时候开始建项目都要触发）"""
        create_project(conn, name="维生素 D")
        assert is_first_project_pending(conn) is True

    def test_验收b_随时可改(self, conn):
        set_lab_name(conn, "检验科")
        assert get_lab_name(conn) == "检验科"
        set_lab_name(conn, "临床免疫室")
        assert get_lab_name(conn) == "临床免疫室"

    def test_验收d_lab_name为空(self, conn):
        with pytest.raises(LabNameEmpty) as exc_info:
            set_lab_name(conn, "")
        assert "实验室名称不能为空" in str(exc_info.value)

    def test_验收d_lab_name为None(self, conn):
        with pytest.raises(LabNameEmpty):
            set_lab_name(conn, None)

    def test_验收d_lab_name全是空格(self, conn):
        with pytest.raises(LabNameEmpty):
            set_lab_name(conn, "   ")

    def test_验收c_保存后立即生效(self, conn):
        """get_lab_name 立即返回新值，模拟'保存后报表抬头等刷新'"""
        set_lab_name(conn, "检验科")
        assert get_lab_name(conn) == "检验科"


# ============================================================================
# 额外：异常消息文本稳定性（issue 文案与 spec 文案）
# ============================================================================

class TestErrorMessages:
    """issue 顶部要求和 §A 边界文案必须严格满足文本一致"""

    def test_项目名空文案(self):
        msg = str(ProjectNameEmpty())
        assert "项目名称不能为空" in msg

    def test_项目名重名文案(self):
        msg = str(ProjectNameExists())
        assert "已存在同名项目" in msg

    def test_实验室名空文案(self):
        msg = str(LabNameEmpty())
        assert "实验室名称不能为空" in msg

    def test_靶值数字格式文案(self):
        msg = str(InvalidNumberFormat("靶值"))
        assert "请输入数字" in msg

    def test_靶值非正文案(self):
        msg = str(NonPositiveNumber("靶值"))
        assert "必须 > 0" in msg
