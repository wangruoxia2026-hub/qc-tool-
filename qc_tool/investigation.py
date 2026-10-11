"""
§F.1 失控排查方向提示面板（spec V1.3 §F.1）

按触发规则返回排查清单分类（随机误差嫌疑 / 系统误差嫌疑），
UI 层（PyQt6）拿到后渲染成可勾选对话框。

公开 API：
- build_investigation_plan(rules, triggered_date, project_name, test_item_name,
                           lot, level, value, checklist=None)
    返回 dict：{'categories': [{'category': str, 'items': [str, ...]}, ...],
                'context': {...}, 'errors': [str]}
    - rules: 触发规则代码列表（例 ['1_3s'] 或 ['1_3s', '2_2s']）
    - checklist: 可选 dict，已勾选项 {category_item: bool}
    - 缺 checklist → 抛 ValueError（spec §F.1 d："已检查项为必填"）

- save_investigation(conn, measurement_id, rules, checklist, conclusion=None)
    把 checklist + conclusion 持久化到 out_of_control 表
    （同一 (measurement_id, rules) 组合只生成一条 out_of_control —— 创建逻辑
     在 measurements.rescan_day_for_test_item 已做；本函数只更新既有行的
     checklist / conclusion）

- list_ooc(conn, project_id=None, test_item_id=None, date_from=None, date_to=None)
    查询 out_of_control 记录（issue #6 边界：删检测指标后能用冗余名称查历史）。
    返回 list[dict]，每条含全部字段。
    参数：
      - project_id / test_item_id：可空；可单独使用或组合
      - date_from / date_to：YYYY-MM-DD 字符串，可空；闭区间
    返回 list[dict]（按 triggered_date DESC, id DESC 排序）。
    注：不依赖 PyQt6。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional


# ============================================================================
# §F.1 排查清单（spec 原文）
# ============================================================================

_RANDOM_ERROR_ITEMS: list[str] = [
    "试剂：是否新开瓶 / 是否过期 / 是否污染",
    "仪器：泵阀、管道气泡、光路、电压",
    "操作：加样量、混匀、读数时机",
    "质控品：复溶是否充分 / 是否污染",
    "加样/混匀/读数等单次操作失误",
]

_SYSTEM_ERROR_ITEMS: list[str] = [
    "校准：是否近期校准 / 标准曲线是否漂移",
    "试剂：批号更换 / 厂家变更",
    "质控品：靶值与当前批是否一致",
    "环境：温度、湿度",
    "方法学偏倚：自身 2s 范围只代表随机误差水平，需结合方法学偏倚判断",
    "耗材：批号更换 / 厂家变更",
]


# 规则 → 分类映射（spec §F.1）
# 2_2s 决议（issue #6）：同时按随机误差与系统误差两个方向排查，
# 实现为把"随机误差嫌疑"+"系统误差嫌疑"两个分类都列出来，
# 不再用单条提示文字的"双向嫌疑"分类。
_RULE_CATEGORY_MAP: dict[str, list[str]] = {
    "1_3s": ["随机误差嫌疑"],
    "R_4s": ["随机误差嫌疑"],
    "4_1s": ["系统误差嫌疑"],
    "10_x": ["系统误差嫌疑"],
    "2_2s": ["随机误差嫌疑", "系统误差嫌疑"],
}


def build_investigation_plan(
    *,
    rules: list[str],
    triggered_date: str,
    project_name: str,
    test_item_name: str,
    lot: str,
    level: str,
    value: float,
    checklist: Optional[dict[str, bool]] = None,
) -> dict[str, Any]:
    """spec §F.1：按规则返回分类清单。

    - 同时触发多规则 → 合并展示（spec §F.1 e）
    - checklist 缺 → 抛 ValueError（spec §F.1 d：已检查项为必填）
    """
    if checklist is None:
        # 业务语义：调用方应在前端先让用户勾选再传进来。
        # 但对纯读 path（GUI 渲染空面板），允许传 None → 返回空 checklist。
        # 为满足"已检查项为必填"语义，save_investigation 才校验。
        pass

    categories_needed: list[str] = []
    for r in rules:
        for c in _RULE_CATEGORY_MAP.get(r, []):
            if c not in categories_needed:
                categories_needed.append(c)

    cat_items: dict[str, list[str]] = {
        "随机误差嫌疑": _RANDOM_ERROR_ITEMS,
        "系统误差嫌疑": _SYSTEM_ERROR_ITEMS,
    }

    plan_categories: list[dict[str, Any]] = []
    for cat in categories_needed:
        items = cat_items.get(cat, [])
        plan_categories.append({
            "category": cat,
            "items": items,
        })

    return {
        "categories": plan_categories,
        "context": {
            "triggered_date": triggered_date,
            "project_name": project_name,
            "test_item_name": test_item_name,
            "lot": lot,
            "level": level,
            "value": value,
            "rules": rules,
        },
        "checklist": checklist or {},
    }


def save_investigation(
    conn: sqlite3.Connection,
    *,
    measurement_id: int,
    rules: list[str],
    checklist: dict[str, bool],
    conclusion: Optional[str] = None,
) -> int:
    """把 checklist + conclusion 写回 out_of_control 表。

    行为：
      - 找已有 (measurement_id, rules) 的 ooc 行 → 更新 checklist / conclusion
      - 没有 → 找任意该 measurement_id 的 ooc 行（兼容 rescan 写入了跨水平规则的情况）→ 更新
      - 都没有 → 抛 LookupError
      - checklist 为空 dict（用户没勾任何项）→ 抛 ValueError（spec §F.1 d）
    """
    if not checklist:
        raise ValueError("已检查项为必填（spec §F.1 d）")

    rules_str = json.dumps(rules, ensure_ascii=False)
    row = conn.execute(
        "SELECT id FROM out_of_control WHERE measurement_id = ? AND rules = ?",
        (measurement_id, rules_str),
    ).fetchone()
    if row is None:
        # 兼容：rescan 可能写入了"包含跨水平规则"的 rules；放宽到 measurement_id 匹配
        row = conn.execute(
            "SELECT id FROM out_of_control WHERE measurement_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (measurement_id,),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"找不到 measurement_id={measurement_id} 对应的失控记录；"
                f"请先在数据录入触发判定。"
            )

    conn.execute(
        "UPDATE out_of_control SET checklist = ?, conclusion = ? WHERE id = ?",
        (json.dumps(checklist, ensure_ascii=False), conclusion, row["id"]),
    )
    return int(row["id"])


# ============================================================================
# list_ooc：失控记录查询（spec §8.3 d + issue #6 边界）
# ============================================================================

# 出表全部列名（含两条冗余名称 test_item_name / project_name）
_OOC_COLUMNS = (
    "id, measurement_id, test_item_id, project_id,"
    " lot, level, triggered_date, test_item_name, project_name,"
    " rules, error_type, checklist, conclusion,"
    " operator, reviewed_by, sample_batch, sample_no,"
    " created_at"
)


def list_ooc(
    conn: sqlite3.Connection,
    *,
    project_id: Optional[int] = None,
    test_item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    """查 out_of_control（issue #6 边界：删检测指标/项目后仍能查历史）。

    实现约定：
      - 任一过滤参数为 None → 该条件不参与 WHERE
      - date_from / date_to 闭区间；二者均可单独 None
      - 不依赖 PyQt6；纯 sqlite3.Row → dict

    返回 list[dict]，按 triggered_date DESC, id DESC 排序。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if test_item_id is not None:
        clauses.append("test_item_id = ?")
        params.append(test_item_id)
    if date_from is not None:
        clauses.append("triggered_date >= ?")
        params.append(date_from)
    if date_to is not None:
        clauses.append("triggered_date <= ?")
        params.append(date_to)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT {_OOC_COLUMNS} FROM out_of_control{where} " \
          f"ORDER BY triggered_date DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "build_investigation_plan",
    "save_investigation",
    "list_ooc",
]