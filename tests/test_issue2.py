"""
issue #2 — 功能 A 验收（spec V1.3 §A.1–A.5 + §8.3）测试先行

12 场景覆盖：
  1   新建项目"维生素 D"，填好单位/厂家 → 保存成功，列表里出现
  2   再建一个同名"维生素 D" → 弹"已存在同名项目"，不入库
  3   项目名留空就保存 → 拒绝保存并提示
  4   在项目下建指标：批号 L001、水平 L、靶值 100、SD 2 → 保存成功
  5   靶值填 0 或 -1 → 弹"靶值必须 > 0"，不入库
  6   靶值填"abc" → 输入框红框提示，不入库
  7   SD 填 0 → 弹"SD 必须 > 0"，不入库
  8   把靶值从 100 改成 102 → 中心线移到 102，历史判定不重算
  9   删除一个有未导出失控记录的指标 → 弹"请先导出失控记录再删除"，删不掉
 10   导出后再删该指标 → 删除成功；measurements 跟着删，out_of_control 保留
 11   给指标添加新批号 L002 → 新批号生效，规则历史清零
 12   全新安装后第一次创建项目 → 强制弹"实验室名称"设置，不填不许继续

TDD 阶段说明（issue #2 明确"校验/增删改函数内部留空直接返回 None，测试全部失败是正常的"）：

  - **校验层** validate_project / validate_test_item / validate_lab_name **本期已实现**
    （GUI 弹框要靠它，不实现就无法接按钮）。
  - **CRUD 层** create_project / create_test_item / delete_test_item / set_lab_name / 
    add_lot / has_unexported_ooc 等 **本期是 stub**，直接 return None / False。
    因此**端到端断言**（场景 1、4、9、10、12 等的"入库后看得见 / 删得掉"）**应该失败**，
    即 TDD "红灯"状态。下一版接 DB 后变绿。

本测试集合按以上语义分两类：
  - 校验/文案测试：绿（校验已实现）
  - CRUD 端到端测试：红（CRUD 是 stub）—— 这是本期的正常现象
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, List

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P


# ============================================================================
# Fixture：每次测试拿到一个全新的临时 qc.db
# ============================================================================

@pytest.fixture
def fresh_db(tmp_path: Path):
    """建库 + 返回一个连接；session 末关闭。"""
    db = tmp_path / "qc.db"
    init_database(db)
    conn = get_connection(db)
    try:
        yield conn
    finally:
        conn.close()


def _seed_project(conn: sqlite3.Connection, name: str = "维生素 D") -> int:
    """直接 INSERT 一个项目，绕过 stub；返回 project_id。"""
    cur = conn.execute(
        "INSERT INTO projects(name, created_at) VALUES (?, ?)",
        (name, "2026-10-11T00:00:00"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_test_item(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: str = "D2",
    lot: str = "L001",
    level: str = "L",
    target: float = 100.0,
    sd: float = 2.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO test_items("
        "  project_id, name, lot, level, target, sd, source, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, '固定', ?)",
        (project_id, name, lot, level, target, sd, "2026-10-11T00:00:00"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_measurement(
    conn: sqlite3.Connection,
    *,
    test_item_id: int,
    project_id: int,
    lot: str = "L001",
    level: str = "L",
    date: str = "2026-04-01",
    value: float = 12.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO measurements("
        "  test_item_id, project_id, lot, level, date, value"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (test_item_id, project_id, lot, level, date, value),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_ooc(
    conn: sqlite3.Connection,
    *,
    test_item_id: int | None,
    project_id: int | None,
    lot: str = "L001",
    level: str = "L",
    triggered_date: str = "2026-04-01",
    test_item_name: str = "D2",
    project_name: str = "维生素 D",
    conclusion: str = "复测合格",
    measurement_id: int | None = None,
) -> int:
    """插一条 out_of_control 记录，模拟已存在的失控记录。"""
    cur = conn.execute(
        "INSERT INTO out_of_control("
        "  measurement_id, test_item_id, project_id,"
        "  lot, level, triggered_date, test_item_name, project_name,"
        "  rules, conclusion"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[\"1_3s\"]', ?)",
        (
            measurement_id, test_item_id, project_id,
            lot, level, triggered_date,
            test_item_name, project_name,
            conclusion,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ============================================================================
# 场景 1：新建项目"维生素 D"，填好单位/厂家 → 保存成功，列表里出现
# ============================================================================

class TestScenario1:
    """场景 1：端到端 —— CRUD 是 stub，本期应当红灯"""

    def test_01_校验通过_stub当前不入库(self, fresh_db):
        """validate_project 通过；create_project stub 不入库，红灯。"""
        # validate 已实现 → 校验通过
        assert P.validate_project(
            name="维生素 D", short="Vit-D", unit="ng/mL", manufacturer="Roche"
        ) is True

        # CRUD stub：当前 return None，未真正写库
        result = P.create_project(
            fresh_db, name="维生素 D",
            short="Vit-D", unit="ng/mL", manufacturer="Roche",
        )
        # 红：stub 未实现，期望 result 是 Project 实例 / True（"保存成功"）
        assert result is not None, \
            "CRUD stub 当前返回 None，期望返回 Project（'保存成功'）"

    def test_01_列表里出现_stub当前空列表(self, fresh_db):
        """list_projects stub 返空，红灯。"""
        # 实际直接 INSERT 一行（绕过 stub）
        pid = _seed_project(fresh_db, "维生素 D")
        assert pid > 0  # seed 确实写了

        # 红：CRUD stub 返回空列表
        result = P.list_projects(fresh_db)
        assert len(result) == 1 and result[0].name == "维生素 D", \
            f"list_projects stub 当前返回 {result!r}，期望看到刚创建的'维生素 D'"


# ============================================================================
# 场景 2：再建一个同名"维生素 D" → 弹"已存在同名项目"，不入库
# ============================================================================

class TestScenario2:
    """场景 2：重名校验（端到端涉及 DB UNIQUE，红灯；文案快照绿）"""

    def test_02_重名文案(self):
        """文案快照：必须含 '已存在同名项目'"""
        assert "已存在同名项目" in str(P.ProjectNameExists())

    def test_02_校验通过_字段层面不查重(self):
        """validate 是纯函数，不读 DB，因此同名字段照样通过"""
        # 同名通过 validate（DB UNIQUE 冲突是另一层）
        assert P.validate_project(name="维生素 D") is True
        assert P.validate_project(name="维生素 D") is True

    def test_02_stub_create重复名_不抛_红(self, fresh_db):
        """CRUD stub 未接 DB UNIQUE，因此重名不抛 ProjectNameExists，红灯。"""
        _seed_project(fresh_db, "维生素 D")
        # 红：期望抛 ProjectNameExists
        with pytest.raises(P.ProjectNameExists):
            P.create_project(fresh_db, name="维生素 D")


# ============================================================================
# 场景 3：项目名留空 → 拒绝保存并提示
# ============================================================================

class TestScenario3:
    """场景 3：项目名空（校验层已实现，绿）"""

    def test_03_projectnameempty文案(self):
        assert "项目名称不能为空" in str(P.ProjectNameEmpty())

    def test_03_validate_project空串(self):
        with pytest.raises(P.ProjectNameEmpty):
            P.validate_project(name="")

    def test_03_validate_project全空格(self):
        with pytest.raises(P.ProjectNameEmpty):
            P.validate_project(name="   ")

    def test_03_validate_project_none(self):
        with pytest.raises(P.ProjectNameEmpty):
            P.validate_project(name=None)

    def test_03_create_project空名抛(self, fresh_db):
        with pytest.raises(P.ProjectNameEmpty):
            P.create_project(fresh_db, name="")

    def test_03_create_project空白名抛(self, fresh_db):
        with pytest.raises(P.ProjectNameEmpty):
            P.create_project(fresh_db, name="    ")


# ============================================================================
# 场景 4：在项目下建指标：批号 L001、水平 L、靶值 100、SD 2 → 保存成功
# ============================================================================

class TestScenario4:
    """场景 4：建指标全部填好 —— CRUD stub 红灯"""

    def test_04_validate_test_item全合法(self):
        assert P.validate_test_item(
            name="维生素 D2", lot="L001", level="L",
            target=100.0, sd=2.0,
        ) is True

    def test_04_靶值整数100通过(self):
        assert P.validate_test_item(
            name="D2", lot="L001", level="L", target=100, sd=2.0,
        ) is True

    def test_04_stub_create不入库_红(self, fresh_db):
        """CRUD stub 不入库，红灯。"""
        _seed_project(fresh_db, "维生素 D")
        # 红：期望 create_test_item 返回 TestItem 实例
        result = P.create_test_item(
            fresh_db, project_id=1,
            name="维生素 D2", lot="L001", level="L",
            target=100.0, sd=2.0,
        )
        assert result is not None, "stub 当前返回 None，期望入库并返回 TestItem"

        # 红：stub 当前返 []，期望看到 D2
        items = P.list_test_items(fresh_db, 1)
        names = [i.name for i in items]
        assert "维生素 D2" in names, f"list_test_items 应返回'D2'，实际 {names}"


# ============================================================================
# 场景 5：靶值填 0 或 -1 → 弹"靶值必须 > 0"，不入库
# ============================================================================

class TestScenario5:
    """场景 5：靶值非正（校验已实现，绿）"""

    def test_05_targetmustpositive文案(self):
        assert "靶值必须 > 0" in str(P.TargetMustPositive())

    def test_05_validate_target0抛(self):
        with pytest.raises(P.TargetMustPositive):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target=0, sd=2.0
            )

    def test_05_validate_target负数抛(self):
        with pytest.raises(P.TargetMustPositive):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target=-1, sd=2.0
            )

    def test_05_validate_target负小数抛(self):
        with pytest.raises(P.TargetMustPositive):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target=-0.01, sd=2.0
            )

    def test_05_create_target0_抛(self, fresh_db):
        with pytest.raises(P.TargetMustPositive):
            P.create_test_item(
                fresh_db, 1, name="D2", lot="L001", level="L",
                target=0, sd=2.0,
            )

    def test_05_create_target负数_抛(self, fresh_db):
        with pytest.raises(P.TargetMustPositive):
            P.create_test_item(
                fresh_db, 1, name="D2", lot="L001", level="L",
                target=-5, sd=2.0,
            )


# ============================================================================
# 场景 6：靶值填"abc" → 输入框红框提示（请输入数字），不入库
# ============================================================================

class TestScenario6:
    """场景 6：靶值非数字（校验已实现，绿）"""

    def test_06_targetinvalidnumber文案含请输入数字(self):
        assert "请输入数字" in str(P.TargetInvalidNumber())

    def test_06_validate_target_字符串abc抛(self):
        with pytest.raises(P.TargetInvalidNumber):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target="abc", sd=2.0
            )

    def test_06_validate_target_空字符串抛(self):
        with pytest.raises((P.TargetMustPositive, P.TargetInvalidNumber)):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target="", sd=2.0
            )

    def test_06_validate_sd_字符串抛(self):
        with pytest.raises(P.SDInvalidNumber):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target=100, sd="xyz"
            )

    def test_06_create_target_abc_抛(self, fresh_db):
        with pytest.raises(P.TargetInvalidNumber):
            P.create_test_item(
                fresh_db, 1, name="D2", lot="L001", level="L",
                target="abc", sd=2.0,
            )

    def test_06_create_target_数字夹杂_抛(self, fresh_db):
        with pytest.raises(P.TargetInvalidNumber):
            P.create_test_item(
                fresh_db, 1, name="D2", lot="L001", level="L",
                target="12abc", sd=2.0,
            )


# ============================================================================
# 场景 7：SD 填 0 → 弹"SD 必须 > 0"，不入库
# ============================================================================

class TestScenario7:
    """场景 7：SD 非正（校验已实现，绿）"""

    def test_07_sdmustpositive文案(self):
        assert "SD 必须 > 0" in str(P.SDMustPositive())

    def test_07_validate_sd_0_抛(self):
        with pytest.raises(P.SDMustPositive):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target=100.0, sd=0
            )

    def test_07_validate_sd_负数_抛(self):
        with pytest.raises(P.SDMustPositive):
            P.validate_test_item(
                name="D2", lot="L001", level="L", target=100.0, sd=-1
            )

    def test_07_create_sd_0_抛(self, fresh_db):
        with pytest.raises(P.SDMustPositive):
            P.create_test_item(
                fresh_db, 1, name="D2", lot="L001", level="L",
                target=100.0, sd=0,
            )


# ============================================================================
# 场景 8：把靶值从 100 改成 102 → 中心线移到 102，历史判定不重算
# ============================================================================

class TestScenario8:
    """场景 8：UI 相关，本期跳过"""

    def test_08_跳过UI(self):
        pytest.skip(
            "场景 8（改靶值中心线移动 + 历史判定不重算）属 GUI / 质控图范畴，"
            "issue #2 明确'界面先不做'；验证由功能 E 负责。"
        )


# ============================================================================
# 场景 9：删除一个有未导出失控记录的指标 → 弹"请先导出失控记录再删除"
# ============================================================================

class TestScenario9:
    """场景 9：未导出 OOC 阻塞删除 —— CRUD stub 红灯"""

    def test_09_hasunexportedooc文案(self):
        assert "请先导出失控记录再删除" in str(P.HasUnexportedOOC())

    def test_09_stub当前has_unexported永远返False_红(self, fresh_db):
        """CRUD stub 未接 DB：has_unexported_ooc 永远 False，红灯（应返 True）。"""
        pid = _seed_project(fresh_db)
        ti = _seed_test_item(fresh_db, pid)
        _seed_ooc(fresh_db, test_item_id=ti, project_id=pid)

        # 红：期望 True（因为 DB 里确实有 ooc）
        assert P.has_unexported_ooc(fresh_db, ti) is True, \
            "stub 当前返回 False，期望 True（DB 中已存在 ooc）"

    def test_09_stub当前delete_test_item不抛_红(self, fresh_db):
        """CRUD stub：delete_test_item 不抛 HasUnexportedOOC，直接返 EmptySummary，红灯。"""
        pid = _seed_project(fresh_db)
        ti = _seed_test_item(fresh_db, pid)
        _seed_ooc(fresh_db, test_item_id=ti, project_id=pid)

        # 红：期望抛 HasUnexportedOOC
        with pytest.raises(P.HasUnexportedOOC):
            P.delete_test_item(fresh_db, ti)


# ============================================================================
# 场景 10：导出后再删该指标 → 删除成功；measurements 跟着删，out_of_control 保留
# ============================================================================

class TestScenario10:
    """场景 10：删除指标的级联行为 —— CRUD stub 红灯；raw SQL 级联断言绿"""

    def test_10_stub_delete不真删_红(self, fresh_db):
        """CRUD stub 不真删任何东西，红灯（期望真删、返回 summary）。"""
        pid = _seed_project(fresh_db, "维生素 D")
        ti = _seed_test_item(fresh_db, pid, lot="L001", level="L")
        mid = _seed_measurement(fresh_db, test_item_id=ti, project_id=pid)
        _seed_ooc(
            fresh_db,
            measurement_id=mid, test_item_id=ti, project_id=pid,
            test_item_name="维生素 D2", project_name="维生素 D",
        )

        summary = P.delete_test_item(fresh_db, ti)

        # 红：CRUD stub 应该返回 summary.n_measurements == 1
        assert isinstance(summary, P.DeleteSummary)
        assert summary.n_measurements == 1, \
            f"summary 应含 n_measurements=1，实际 {summary}"

        # 红：CRUD stub 应真删 test_item
        assert _count_rows(fresh_db, "test_items") == 0, \
            "test_item 1 应被真删"

        # 红：measurements 应 CASCADE 消失
        assert _count_rows(fresh_db, "measurements") == 0

        # 绿：out_of_control 保留（与 CRUD 是否实现无关，由 FK 决定）
        assert _count_rows(fresh_db, "out_of_control") == 1, \
            "out_of_control 应保留（spec §8.3 c 要求）"

    def test_10_cascade语义_by_raw_sql_绿(self, fresh_db):
        """直接用 SQL 验证 spec §8.3 的级联行为 —— 这部分依赖 db.py，不是 stub"""
        pid = _seed_project(fresh_db, "维生素 D")
        ti = _seed_test_item(fresh_db, pid, lot="L001", level="L",
                              target=100.0, sd=2.0)
        mid = _seed_measurement(
            fresh_db, test_item_id=ti, project_id=pid,
            lot="L001", level="L", date="2026-04-01", value=12.0,
        )
        ooc_id = _seed_ooc(
            fresh_db,
            measurement_id=mid, test_item_id=ti, project_id=pid,
            lot="L001", level="L", triggered_date="2026-04-01",
            test_item_name="维生素 D2", project_name="维生素 D",
            conclusion="复测合格",
        )

        fresh_db.execute("DELETE FROM test_items WHERE id = ?", (ti,))
        fresh_db.commit()

        # measurements CASCADE
        n_meas = fresh_db.execute(
            "SELECT COUNT(*) FROM measurements WHERE test_item_id = ?", (ti,)
        ).fetchone()[0]
        assert n_meas == 0

        # out_of_control 保留 + 冗余 name 字段仍在
        row = fresh_db.execute(
            "SELECT measurement_id, test_item_id, project_id,"
            "       lot, level, triggered_date, test_item_name,"
            "       project_name, rules, conclusion "
            "FROM out_of_control WHERE id = ?",
            (ooc_id,),
        ).fetchone()
        assert row is not None
        assert row["measurement_id"] is None  # SET NULL
        assert row["test_item_name"] == "维生素 D2"
        assert row["project_name"] == "维生素 D"
        assert row["lot"] == "L001"
        assert row["level"] == "L"
        assert row["triggered_date"] == "2026-04-01"
        assert row["rules"] == '["1_3s"]'
        assert row["conclusion"] == "复测合格"

    def test_10_ooc_test_item_id_无FK不自动置空_绿(self, fresh_db):
        """spec §8.1：out_of_control.test_item_id 不加 FK（避免自动置空）"""
        pid = _seed_project(fresh_db, "维生素 D")
        ti = _seed_test_item(fresh_db, pid)
        ooc_id = _seed_ooc(
            fresh_db, test_item_id=ti, project_id=pid,
        )

        fresh_db.execute("DELETE FROM test_items WHERE id = ?", (ti,))
        fresh_db.commit()

        row = fresh_db.execute(
            "SELECT test_item_id FROM out_of_control WHERE id = ?", (ooc_id,)
        ).fetchone()
        assert row["test_item_id"] == ti


# ============================================================================
# 场景 11：给指标添加新批号 L002
# ============================================================================

class TestScenario11:
    """场景 11：UI 相关（涉及控件 + 列表刷新）"""

    def test_11_跳过UI(self):
        pytest.skip(
            "场景 11（添加新批号 + 规则历史清零）涉及 GUI 控件，"
            "issue #2 明确'界面先不做'；add_lot stub 已留接口。"
        )


# ============================================================================
# 场景 12：全新安装后第一次创建项目 → 强制弹出"实验室名称"设置
# ============================================================================

class TestScenario12:
    """场景 12：首次创建项目时强制实验室名称 —— CRUD stub 红灯；validate 绿"""

    def test_12_labnameempty文案(self):
        assert "实验室名称不能为空" in str(P.LabNameEmpty())

    def test_12_validate_lab_name_空(self):
        with pytest.raises(P.LabNameEmpty):
            P.validate_lab_name(name="")

    def test_12_validate_lab_name_全空白(self):
        with pytest.raises(P.LabNameEmpty):
            P.validate_lab_name(name="   ")

    def test_12_validate_lab_name_none(self):
        with pytest.raises(P.LabNameEmpty):
            P.validate_lab_name(name=None)

    def test_12_validate_lab_name_合法通过(self):
        assert P.validate_lab_name(name="检验科") is True

    def test_12_set_lab_name真正写入lab_settings_红(self, fresh_db):
        """CRUD stub 不写真 DB，红灯（期望看到 '检验科'）。"""
        # 调用方应先 validate 通过
        P.validate_lab_name(name="检验科")

        # stub 调用
        P.set_lab_name(fresh_db, "检验科")

        # 红：期望库中 lab_settings.lab_name = '检验科'
        row = fresh_db.execute(
            "SELECT lab_name FROM lab_settings WHERE id = 1"
        ).fetchone()
        assert row is not None and row["lab_name"] == "检验科", \
            f"set_lab_name stub 当前未写库，row={row!r}"

    def test_12_is_first_project_pending全新库返True_红(self, fresh_db):
        """全新库（projects 空 + lab_settings 空）→ 期望返 True，红灯。"""
        # 红：CRUD stub 当前返 False
        assert P.is_first_project_pending(fresh_db) is True

    def test_12_全新库_pending真实语义_by_sql_绿(self, fresh_db):
        """直接 SQL 验证全新库的判定状态（与 CRUD stub 无关）"""
        n_lab = fresh_db.execute(
            "SELECT COUNT(*) FROM lab_settings WHERE lab_name IS NOT NULL"
        ).fetchone()[0]
        n_proj = fresh_db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        assert n_lab == 0 and n_proj == 0, "全新库的 SQL 状态应为 'pending'"


# ============================================================================
# 异常文案稳定性回归（issue #2 列出的文案必须稳定）
# ============================================================================

class TestExceptionSnapshots:
    """9 条异常文案快照 —— 防止后续重构改动文案"""

    @pytest.mark.parametrize("cls,expected", [
        (P.ProjectNameEmpty, "项目名称不能为空"),
        (P.ProjectNameExists, "已存在同名项目"),
        (P.TargetMustPositive, "靶值必须 > 0"),
        (P.SDMustPositive, "SD 必须 > 0"),
        (P.TargetInvalidNumber, "请输入数字"),
        (P.SDInvalidNumber, "请输入数字"),
        (P.LabNameEmpty, "实验室名称不能为空"),
        (P.HasUnexportedOOC, "请先导出失控记录再删除"),
        (P.LevelInvalid, "水平必须为 L 或 H"),
    ])
    def test_异常文案(self, cls, expected):
        assert expected in str(cls())
