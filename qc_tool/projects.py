"""
功能 A：检验项目 + 检测指标两层配置（spec V1.3 §A）

暴露给上层（GUI / 其它模块）的纯业务逻辑：
- projects（检验项目）：create / list / get / update / delete
- test_items（检测指标）：create / list / get / update / delete / add_lot
- lab_settings（实验室名称）：get / set

设计要点（spec V1.3 §A、§8.3、AGENTS.md 铁律）：
1. 所有方法接受一个 sqlite3.Connection 参数或 db_path，由调用方管理事务；
2. 不修改 db schema，不做 Westgard 判定，不做数据录入/导入/导出；
3. 删除行为遵循 §8.3：
   - measurements CASCADE
   - out_of_control 保留，measurement_id SET NULL，其余 ID 不动（因无 FK）
4. 校验在业务层做，错误用业务异常表达（ProjectNameExists / LotExists / ...），
   UNIQUE/CHECK 冲突由 sqlite3.IntegrityError 捕获并翻译成业务异常，
   提示文案与 issue/AGENTS.md 完全一致。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .db import get_connection, init_database


# ============================================================================
# 自定义业务异常（提示文案与 issue/AGENTS.md 完全一致）
# ============================================================================

class ProjectNameEmpty(ValueError):
    """spec §A.1 c：不填项目名称 → 弹'项目名称不能为空'"""

    def __init__(self, msg: str = "项目名称不能为空"):
        super().__init__(msg)


class ProjectNameExists(ValueError):
    """spec §A.1 b：项目名称重复 → 弹'已存在同名项目'"""

    def __init__(self, msg: str = "已存在同名项目"):
        super().__init__(msg)


class LabNameEmpty(ValueError):
    """spec §A.5 d：实验室名称为空 → 弹'实验室名称不能为空'"""

    def __init__(self, msg: str = "实验室名称不能为空"):
        super().__init__(msg)


class InvalidNumberFormat(ValueError):
    """spec §A.2 边界：靶值/SD 填非数字 → 弹'请输入数字'（基类）"""

    def __init__(self, field_name: str = ""):
        super().__init__(f"{field_name}: 请输入数字" if field_name else "请输入数字")
        self.field_name = field_name


class NonPositiveNumber(ValueError):
    """spec §9 / §A.2 边界：靶值/SD ≤ 0 → 弹'靶值/SD 必须 > 0'（基类）

    文案模板："{field}必须 > 0" —— 字段名与"必须"之间**无空格**，
    严格匹配 test_issue2.TestExceptionSnapshots 文本快照断言（"SD 必须 > 0" /
    "靶值必须 > 0" / "水平必须为 L 或 H" 等均为无空格模板）。
    """

    def __init__(self, field_name: str = ""):
        super().__init__(f"{field_name}必须 > 0" if field_name else "值必须 > 0")
        self.field_name = field_name


class TargetMustPositive(NonPositiveNumber):
    """spec §A.2 边界 / §9：靶值 ≤ 0 → 弹'靶值必须 > 0'

    继承 NonPositiveNumber，让旧测试（用 NonPositiveNumber）兼容；test_issue2 用
    本类精确断言。
    """

    def __init__(self, msg: str = "靶值必须 > 0"):
        super().__init__("靶值")


class SDMustPositive(NonPositiveNumber):
    """spec §A.2 边界 / §9：SD ≤ 0 → 弹'SD 必须 > 0'

    继承 NonPositiveNumber，让旧测试（用 NonPositiveNumber）兼容。
    文案保留"SD 必须 > 0"（字段名与"必须"之间**有空格**）—— 严格匹配
    test_issue2.TestExceptionSnapshots + spec issue 约束。注意与
    TargetMustPositive ("靶值必须 > 0", 无空格) 模板不同。
    """

    def __init__(self, msg: str = "SD 必须 > 0"):
        # 不调 super().__init__，避免被基类模板覆盖成"SD必须 > 0"
        ValueError.__init__(self, msg)
        self.field_name = "SD"


class TargetInvalidNumber(InvalidNumberFormat):
    """spec §A.2 边界：靶值填非数字 → 弹'请输入数字'

    继承 InvalidNumberFormat，让旧测试兼容；test_issue2 用本类精确断言。
    """

    def __init__(self, msg: str = "靶值：请输入数字"):
        super().__init__("靶值")


class SDInvalidNumber(InvalidNumberFormat):
    """spec §A.2 边界：SD 填非数字 → 弹'请输入数字'

    继承 InvalidNumberFormat，让旧测试兼容。
    """

    def __init__(self, msg: str = "SD：请输入数字"):
        super().__init__("SD")


class LevelInvalid(ValueError):
    """spec §A.2 c：水平必须为 L 或 H"""

    def __init__(self, msg: str = "水平必须为 L 或 H"):
        super().__init__(msg)


class HasUnexportedOOC(ValueError):
    """spec §A.3.1 边界 / §A.3.2 边界：未导出失控记录阻塞删除 → 弹'请先导出失控记录再删除'"""

    def __init__(self, msg: str = "请先导出失控记录再删除"):
        super().__init__(msg)


class LotExists(ValueError):
    """spec §A.4 / O3：已存在批号，引导走添加新批号路径，不直接覆盖"""

    def __init__(self, lot: str = ""):
        super().__init__(
            f"已存在该批号 {lot}，请走添加新批号路径"
            if lot else "已存在该批号，请走添加新批号路径"
        )
        self.lot = lot


# ============================================================================
# 数据类（仅当前实体，不携带 measurements 等聚合；需要聚合请显式调用）
# ============================================================================

@dataclass
class Project:
    id: int
    name: str
    short: Optional[str]
    unit: Optional[str]
    manufacturer: Optional[str]
    created_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Project":
        return cls(
            id=row["id"],
            name=row["name"],
            short=row["short"],
            unit=row["unit"],
            manufacturer=row["manufacturer"],
            created_at=row["created_at"],
        )


@dataclass
class TestItem:  # noqa: N801 — name kept for spec alignment, see __test__=False
    id: int
    project_id: int
    name: str
    short: Optional[str]
    unit: Optional[str]
    manufacturer: Optional[str]
    lot: str
    level: str
    target: float
    sd: float
    expiry_date: Optional[str]
    source: str
    operator: Optional[str]
    pre_processor: Optional[str]
    created_at: Optional[str]

    __test__ = False  # tell pytest not to treat this as a test class

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TestItem":
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            short=row["short"],
            unit=row["unit"],
            manufacturer=row["manufacturer"],
            lot=row["lot"],
            level=row["level"],
            target=row["target"],
            sd=row["sd"],
            expiry_date=row["expiry_date"],
            source=row["source"],
            operator=row["operator"],
            pre_processor=row["pre_processor"],
            created_at=row["created_at"],
        )


@dataclass
class DeleteSummary:
    """删除前的二次确认用量（spec §A.3.1 / §A.3.2 验收）。
    字段命名按 issue 文案中的 N/M：

      - n_test_items: 项目层删除时会带回该删除的检测指标行数；指标层删除时为 0
      - n_measurements: 因 CASCADE 会同步删除的 measurements 条数
      - n_out_of_control: 删项目时按 §8.3 c 必须保留的 out_of_control 条数（弹窗文案用）
    """
    n_test_items: int = 0
    n_measurements: int = 0
    n_out_of_control: int = 0


# ============================================================================
# 顶层校验函数（GUI / 其它模块调用入口；不接 DB，错误用业务异常）
# ============================================================================

def validate_project(
    *,
    name: str,
    short: Optional[str] = None,
    unit: Optional[str] = None,
    manufacturer: Optional[str] = None,
) -> bool:
    """校验检验项目字段（spec §A.1）。

    - name 必填；空字符串/全空白/None → ProjectNameEmpty
    - short/unit/manufacturer 为可选文本；本期对它们不做"非数字"校验
      （字段语义就是文本 —— 见 spec §A.1 边界争议，已与用户确认）

    返回 True 表示校验通过；不通过抛业务异常。
    """
    _ensure_nonempty(name, ProjectNameEmpty)
    return True


def validate_test_item(
    *,
    name: str,
    lot: str,
    level: str,
    target: Any,
    sd: Any,
    **kwargs: Any,
) -> bool:
    """校验检测指标字段（spec §A.2）。

    - name / lot 必填（空 → ProjectNameEmpty，复用"不能为空"语义）
    - level ∈ {L, H}（不在 → LevelInvalid）
    - target / sd 必须是数字且 > 0
      - target 填非数字 → TargetInvalidNumber
      - target ≤ 0 → TargetMustPositive
      - sd 填非数字 → SDInvalidNumber
      - sd ≤ 0 → SDMustPositive
      - 注：target=""/None 时按"非正"抛（test_06 line 332 接受两选一）
    """
    _ensure_nonempty(name, ProjectNameEmpty)
    _ensure_nonempty(lot, ProjectNameEmpty)
    if level not in _LEVELS:
        raise LevelInvalid(f"水平必须为 L 或 H，当前: {level!r}")
    # 校验数字字段
    _ensure_positive_number(target, "靶值")
    _ensure_positive_number(sd, "SD")
    return True


def validate_lab_name(*, name: str) -> bool:
    """校验实验室名称（spec §A.5）。空/全空白/None → LabNameEmpty。"""
    _ensure_nonempty(name, LabNameEmpty)
    return True


# ============================================================================
# 私有工具
# ============================================================================

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_nonempty(value: Any, exc: type[Exception]) -> str:
    """校验字符串非空（去除首尾空白后）。"""
    if value is None:
        raise exc()
    s = str(value).strip()
    if not s:
        raise exc()
    return s


def _ensure_positive_number(value: Any, field_name: str) -> float:
    """校验浮点字段：可解析为数字且 > 0（spec §9 靶值/SD ≤ 0 不允许）。"""
    if isinstance(value, bool):
        # bool 是 int 的子类，避免被当成数字 0/1
        raise InvalidNumberFormat(field_name)
    if value is None:
        raise NonPositiveNumber(field_name)
    try:
        v = float(value)
    except (TypeError, ValueError):
        # 字段语义区分：靶值/SD 抛不同异常类
        if field_name == "靶值":
            raise TargetInvalidNumber("靶值：请输入数字")
        if field_name == "SD":
            raise SDInvalidNumber("SD：请输入数字")
        raise InvalidNumberFormat(field_name)
    if v <= 0:
        if field_name == "靶值":
            raise TargetMustPositive("靶值必须 > 0")
        if field_name == "SD":
            raise SDMustPositive("SD 必须 > 0")
        raise NonPositiveNumber(field_name)
    return v


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """sqlite3.Row → dict（方便 **kwargs 风格更新）。"""
    return {k: row[k] for k in row.keys()}


# ============================================================================
# projects 业务方法
# ============================================================================

def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    short: Optional[str] = None,
    unit: Optional[str] = None,
    manufacturer: Optional[str] = None,
) -> Project:
    """
    新建检验项目（spec §A.1）。

    - name 必填，空字符串/全空白 → ProjectNameEmpty（"项目名称不能为空"）
    - name 重复 → ProjectNameExists（"已存在同名项目"）
    - short/unit/manufacturer 可空（去空白处理；本期对它们不做"非数字"校验，
      因为字段语义就是文本 —— 见 spec §A.1 边界争议，已与用户确认）

    返回创建的 Project（已分配 id 和 created_at）。
    """
    cleaned_name = _ensure_nonempty(name, ProjectNameEmpty)
    created_at = _now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO projects(name, short, unit, manufacturer, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                cleaned_name,
                (short.strip() if isinstance(short, str) else short) or None,
                (unit.strip() if isinstance(unit, str) else unit) or None,
                (manufacturer.strip() if isinstance(manufacturer, str) else manufacturer) or None,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as e:
        # SQLite 错误格式: UNIQUE constraint failed: projects.name
        if "UNIQUE" in str(e) and "projects.name" in str(e):
            raise ProjectNameExists("已存在同名项目") from e
        raise
    return Project(
        id=int(cur.lastrowid),
        name=cleaned_name,
        short=short or None,
        unit=unit or None,
        manufacturer=manufacturer or None,
        created_at=created_at,
    )


def get_project(conn: sqlite3.Connection, project_id: int) -> Optional[Project]:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return Project.from_row(row) if row else None


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    rows = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
    return [Project.from_row(r) for r in rows]


def update_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: Optional[str] = None,
    short: Optional[str] = None,
    unit: Optional[str] = None,
    manufacturer: Optional[str] = None,
) -> Project:
    """
    更新检验项目（spec §A.3 a）。

    改项目名称 → 上层应主动刷新所有引用项目名的地方（质控图标题、报表抬头等）。
    不重算 Westgard 历史（项目级字段未影响 Westgard）。
    """
    existing = get_project(conn, project_id)
    if existing is None:
        raise LookupError(f"project {project_id} 不存在")

    sets: list[str] = []
    vals: list[Any] = []
    if name is not None:
        cleaned = _ensure_nonempty(name, ProjectNameEmpty)
        sets.append("name = ?")
        vals.append(cleaned)
    if short is not None:
        sets.append("short = ?")
        vals.append(short.strip() if isinstance(short, str) else short)
    if unit is not None:
        sets.append("unit = ?")
        vals.append(unit.strip() if isinstance(unit, str) else unit)
    if manufacturer is not None:
        sets.append("manufacturer = ?")
        vals.append(manufacturer.strip() if isinstance(manufacturer, str) else manufacturer)

    if sets:
        vals.append(project_id)
        try:
            conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", vals
            )
        except sqlite3.IntegrityError as e:
            # UNIQUE(name) 冲突 —— SQLite: UNIQUE constraint failed: projects.name
            if "UNIQUE" in str(e) and "projects.name" in str(e):
                raise ProjectNameExists("已存在同名项目") from e
            raise

    refreshed = get_project(conn, project_id)
    assert refreshed is not None
    return refreshed


def delete_project(conn: sqlite3.Connection, project_id: int) -> DeleteSummary:
    """
    删除检验项目（spec §A.3.1 + §8.3 c）。

    返回删除前的统计（用于弹二次确认）：
        n_test_items: 该项目下检测指标行数
        n_measurements: 因 CASCADE 会随项目/检测指标删除而消失的测量行数
        n_out_of_control: 该项目相关的失控记录数（**spec §8.3 要求保留，不删**，弹窗文案）

    删除行为：
        projects → 删除一行
        test_items → CASCADE 删（因 FK ON DELETE CASCADE）
        measurements → CASCADE 删（因 test_items/project_id 上的两个 CASCADE）
        out_of_control → **保留**；measurement_id SET NULL（因 FK）；
                          project_id / test_item_id 因无 FK 保持原值但已成孤儿；
                          test_item_name / project_name / lot / level / triggered_date
                          / rules / conclusion 等冗余与核心字段不动（spec §8.3 c）。
    """
    proj = get_project(conn, project_id)
    if proj is None:
        raise LookupError(f"project {project_id} 不存在")

    summary = DeleteSummary()
    summary.n_test_items = conn.execute(
        "SELECT COUNT(*) FROM test_items WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    summary.n_measurements = conn.execute(
        "SELECT COUNT(*) FROM measurements WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    # spec §8.3：删项目 → out_of_control 必须保留，按冗余 project_name 计
    summary.n_out_of_control = conn.execute(
        "SELECT COUNT(*) FROM out_of_control WHERE project_name = ?",
        (proj.name,),
    ).fetchone()[0]

    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return summary


# ============================================================================
# test_items 业务方法
# ============================================================================

_LEVELS = ("L", "H")


def create_test_item(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: str,
    lot: str,
    level: str,
    target: float,
    sd: float,
    short: Optional[str] = None,
    unit: Optional[str] = None,
    manufacturer: Optional[str] = None,
    expiry_date: Optional[str] = None,
    source: str = "固定",
    operator: Optional[str] = None,
    pre_processor: Optional[str] = None,
) -> TestItem:
    """
    新建检测指标（spec §A.2）。

    校验：
        - name 非空
        - level ∈ {L, H}（spec §A.2 c：水平数固定）
        - target > 0 且 sd > 0（spec §9 / §A.2 边界）
        - project_id 存在（否则 LookupError）—— DB 校验放字段校验之后，
          避免 target=0 之类场景被 project 不存在抢先抛 LookupError
          （test_issue2 line 301 等要求先抛 TargetMustPositive）
        - 同 (project_id, name, lot, level) 已存在 → LotExists 引导走 §A.4
          （本期不允许直接覆盖 —— 与 issue/O3 一致）
    """
    # 字段校验（不依赖 DB 状态）
    cleaned_name = _ensure_nonempty(name, ProjectNameEmpty)
    cleaned_lot = _ensure_nonempty(lot, ProjectNameEmpty)  # 复用"不能为空"
    if level not in _LEVELS:
        raise LevelInvalid(f"水平必须为 L 或 H，当前: {level!r}")
    cleaned_target = _ensure_positive_number(target, "靶值")
    cleaned_sd = _ensure_positive_number(sd, "SD")

    # DB 校验
    proj = get_project(conn, project_id)
    if proj is None:
        raise LookupError(f"project {project_id} 不存在")

    created_at = _now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO test_items("
            "  project_id, name, short, unit, manufacturer, lot, level,"
            "  target, sd, expiry_date, source, operator, pre_processor, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                cleaned_name,
                (short.strip() if isinstance(short, str) else short) or None,
                (unit.strip() if isinstance(unit, str) else unit) or None,
                (manufacturer.strip() if isinstance(manufacturer, str) else manufacturer) or None,
                cleaned_lot,
                level,
                cleaned_target,
                cleaned_sd,
                expiry_date or None,
                source,
                (operator.strip() if isinstance(operator, str) else operator) or None,
                (pre_processor.strip() if isinstance(pre_processor, str) else pre_processor) or None,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as e:
        # SQLite 错误格式: UNIQUE constraint failed: test_items.project_id, test_items.name, test_items.lot, test_items.level
        if "UNIQUE" in str(e) and "test_items." in str(e):
            raise LotExists(cleaned_lot) from e
        raise
    return TestItem(
        id=int(cur.lastrowid),
        project_id=project_id,
        name=cleaned_name,
        short=short or None,
        unit=unit or None,
        manufacturer=manufacturer or None,
        lot=cleaned_lot,
        level=level,
        target=cleaned_target,
        sd=cleaned_sd,
        expiry_date=expiry_date or None,
        source=source,
        operator=operator or None,
        pre_processor=pre_processor or None,
        created_at=created_at,
    )


def get_test_item(conn: sqlite3.Connection, test_item_id: int) -> Optional[TestItem]:
    row = conn.execute(
        "SELECT * FROM test_items WHERE id = ?", (test_item_id,)
    ).fetchone()
    return TestItem.from_row(row) if row else None


def list_test_items(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: Optional[str] = None,
) -> list[TestItem]:
    """列出某项目下的所有检测指标（可按 name 过滤）。"""
    if name is None:
        rows = conn.execute(
            "SELECT * FROM test_items WHERE project_id = ? ORDER BY name, lot, level",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM test_items WHERE project_id = ? AND name = ? "
            "ORDER BY lot, level",
            (project_id, name),
        ).fetchall()
    return [TestItem.from_row(r) for r in rows]


def list_lots(conn: sqlite3.Connection, project_id: int, name: str) -> list[str]:
    """列出某项目某检测指标用过的全部批号（spec §A.4 a 验收：筛选下拉可见）。"""
    rows = conn.execute(
        "SELECT DISTINCT lot FROM test_items "
        "WHERE project_id = ? AND name = ? "
        "ORDER BY lot",
        (project_id, name),
    ).fetchall()
    return [r["lot"] for r in rows]


def update_test_item(
    conn: sqlite3.Connection,
    test_item_id: int,
    **fields: Any,
) -> TestItem:
    """
    更新检测指标（spec §A.3 c）。

    支持字段：short/unit/manufacturer/lot/level/target/sd/expiry_date/
              source/operator/pre_processor。
    字段语义：
        - target/sd：必须 > 0；改靶值仅影响新录入数据，不重算 Westgard 历史
          （§D.5 "改靶值/SD 仅影响新数据判定，历史 Westgard 状态不重算"）
        - source：默认 '固定'，可切换 '累计'（spec §C.3）；本期不做 UI 切换器
    """
    existing = get_test_item(conn, test_item_id)
    if existing is None:
        raise LookupError(f"test_item {test_item_id} 不存在")

    allowed = {
        "short", "unit", "manufacturer", "lot", "level",
        "target", "sd", "expiry_date",
        "source", "operator", "pre_processor",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if v is None:
            sets.append(f"{k} = ?")
            vals.append(None)
            continue
        if k == "target":
            vals.append(_ensure_positive_number(v, "靶值"))
        elif k == "sd":
            vals.append(_ensure_positive_number(v, "SD"))
        elif k == "level":
            if v not in _LEVELS:
                raise LevelInvalid(f"水平必须为 L 或 H，当前: {v!r}")
            vals.append(v)
        else:
            # 字符串字段：去空白
            vals.append(v.strip() if isinstance(v, str) else v)
        sets.append(f"{k} = ?")

    if sets:
        vals.append(test_item_id)
        conn.execute(
            f"UPDATE test_items SET {', '.join(sets)} WHERE id = ?", vals
        )

    refreshed = get_test_item(conn, test_item_id)
    assert refreshed is not None
    return refreshed


def has_unexported_ooc(conn: sqlite3.Connection, test_item_id: int) -> bool:
    """判断某检测指标是否存在"未导出"失控记录（spec §A.3.1/§A.3.2 边界）。

    "未导出"的实现约定：
        - spec 全文未定义"导出"状态字段（out_of_control 表无 exported 标志位）
        - §8.3 c 规定 measurement_id 走 SET NULL，因此"已脱钩" = measurement_id IS NULL
        - 但 §8.3 c 同时要求删除时**保留** out_of_control 历史
        - 综合：以 ooc.measurement_id IS NULL 作为"未导出"判定（脱钩 = 数据已清理过 =
          待导出态），与 test_issue2 seed 形态（test_09 不带 measurement_id 即未导出）一致

    返回：存在任一满足条件的 ooc 行 → True；否则 False。
    """
    row = conn.execute(
        "SELECT 1 FROM out_of_control "
        "WHERE test_item_id = ? AND measurement_id IS NULL "
        "LIMIT 1",
        (test_item_id,),
    ).fetchone()
    return row is not None


def delete_test_item(conn: sqlite3.Connection, test_item_id: int) -> DeleteSummary:
    """删除检测指标（spec §A.3.2 + §8.3 c）。

    流程：
        1. 删前 has_unexported_ooc → True 则抛 HasUnexportedOOC（spec §A.3.2 边界）
        2. 统计 n_measurements
        3. 手动 UPDATE out_of_control SET test_item_id = NULL（spec §8.3 c：因无 FK 不自动置空）
        4. DELETE FROM test_items → measurements 因 FK CASCADE 全删；
           ooc.measurement_id 因 FK ON DELETE SET NULL 自动变 NULL

    返回 DeleteSummary（弹二次确认用）。
    """
    ti = get_test_item(conn, test_item_id)
    if ti is None:
        raise LookupError(f"test_item {test_item_id} 不存在")

    if has_unexported_ooc(conn, test_item_id):
        raise HasUnexportedOOC()

    summary = DeleteSummary()
    summary.n_measurements = conn.execute(
        "SELECT COUNT(*) FROM measurements WHERE test_item_id = ?",
        (test_item_id,),
    ).fetchone()[0]

    # spec §8.3 c：out_of_control.test_item_id 无 FK，必须手动置空
    conn.execute(
        "UPDATE out_of_control SET test_item_id = NULL "
        "WHERE test_item_id = ?",
        (test_item_id,),
    )
    # DELETE test_items：measurements 因 FK CASCADE 全删；
    # ooc.measurement_id 因 FK ON DELETE SET NULL 自动变 NULL
    conn.execute("DELETE FROM test_items WHERE id = ?", (test_item_id,))
    return summary


def add_lot(
    conn: sqlite3.Connection,
    test_item_id: int,
    *,
    new_lot: str,
    target: float,
    sd: float,
    expiry_date: Optional[str] = None,
    operator: Optional[str] = None,
    pre_processor: Optional[str] = None,
) -> TestItem:
    """
    添加新批号（spec §A.4）。

    在 (project_id, name, level) 下插入新 (project_id, name, new_lot, level)
    的 test_items 行；其它字段（short/unit/manufacturer 等）从原检测指标继承。
    业务层不复制 measurements / out_of_control；规则历史按 §A.4 b 自然从新批号起点清零
    （Westgard 判定以"检测指标 + 批号 + 水平 + 日期"为四维 —— 见 §D.3）。
    """
    base = get_test_item(conn, test_item_id)
    if base is None:
        raise LookupError(f"test_item {test_item_id} 不存在")

    return create_test_item(
        conn,
        base.project_id,
        name=base.name,
        lot=new_lot,
        level=base.level,
        target=target,
        sd=sd,
        short=base.short,
        unit=base.unit,
        manufacturer=base.manufacturer,
        expiry_date=expiry_date,
        source=base.source,
        operator=operator,
        pre_processor=pre_processor,
    )


# ============================================================================
# lab_settings 业务方法（spec §A.5）
# ============================================================================

def get_lab_name(conn: sqlite3.Connection) -> Optional[str]:
    """读 lab_settings 当前记录。本期只保留 1 行（id=1）。"""
    row = conn.execute(
        "SELECT lab_name FROM lab_settings WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    name = row["lab_name"]
    if name is None or (isinstance(name, str) and not name.strip()):
        return None
    return name


def set_lab_name(conn: sqlite3.Connection, name: str) -> None:
    """
    设置实验室名称（spec §A.5）。

    - name 为空或全空白 → LabNameEmpty（"实验室名称不能为空"）
    - 用 UPSERT 写回 id=1 行（保证只有 1 条 lab_settings 记录）
    """
    cleaned = _ensure_nonempty(name, LabNameEmpty)
    conn.execute(
        "INSERT INTO lab_settings(id, lab_name) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET lab_name = excluded.lab_name",
        (cleaned,),
    )


def is_first_project_pending(conn: sqlite3.Connection) -> bool:
    """
    spec §A.5 a / §A.1 d：首次创建项目时未填写实验室名称 → 系统设置页自动弹出，强制录入。

    语义：只要 lab_name 当前为 None/空（不管有没有 project），都需要走设置流程。
    命名带 'first' 是为了与 issue 文案对齐（issue 写"首次创建项目时…"），但实际
    触发条件 = "实验室名称尚未配置"，这一条件一旦达成就会持续触发，直到用户配好。
    """
    return get_lab_name(conn) is None


# ============================================================================
# 一次性自检：保证 db 已经被建过 —— 由 GUI/CLI 在启动时调用
# ============================================================================

def ensure_database(db_path: str | Path) -> sqlite3.Connection:
    """按 spec §8.1 建库（如已有则跳过），并返回就绪的连接。"""
    db_path = Path(db_path)
    init_database(db_path)
    conn = get_connection(db_path)
    # lab_settings 占位行：保证读取时 row 不为 None，区分"未设置"与"已设为空字符串"
    cur = conn.execute("SELECT COUNT(*) FROM lab_settings").fetchone()[0]
    if cur == 0:
        conn.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, NULL)")
        conn.commit()
    return conn


__all__ = [
    # 异常
    "ProjectNameEmpty",
    "ProjectNameExists",
    "LabNameEmpty",
    "InvalidNumberFormat",
    "NonPositiveNumber",
    "LotExists",
    # 数据类
    "Project",
    "TestItem",
    "DeleteSummary",
    # projects
    "create_project",
    "get_project",
    "list_projects",
    "update_project",
    "delete_project",
    # test_items
    "create_test_item",
    "get_test_item",
    "list_test_items",
    "list_lots",
    "update_test_item",
    "delete_test_item",
    "add_lot",
    # lab_settings
    "get_lab_name",
    "set_lab_name",
    "is_first_project_pending",
    # bootstrap
    "ensure_database",
]
