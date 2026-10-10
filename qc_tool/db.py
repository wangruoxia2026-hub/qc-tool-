"""
数据库连接与建表。

建表 SQL 必须与 spec §8.1 一字不差（字段名 / 类型 / UNIQUE / 外键 / ON DELETE 一律不动）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# spec §8.1 —— 5 张表的 DDL，原样抄录，不许增删改任何字符
SCHEMA_SQL = """
-- 检验项目表（V1.3 新拆：只存项目级元信息，不存质控数据）
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,                -- 检验项目名称（如"维生素 D"）
  short TEXT,                        -- 简称
  unit TEXT,                         -- 默认单位（检测指标可覆盖）
  manufacturer TEXT,                 -- 默认厂家（检测指标可覆盖）
  created_at TEXT,
  UNIQUE(name)                       -- 项目名称唯一
);

-- 检测指标表（V1.3 新增：每个项目下挂多个检测指标，每个指标独立配 2 水平 + 批号 + 靶值 + SD）
CREATE TABLE IF NOT EXISTS test_items(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL,       -- 指向 projects.id
  name TEXT NOT NULL,                -- 检测指标名称（如"维生素 D2"、"D3"）
  short TEXT,                        -- 简称
  unit TEXT,                         -- 单位（如 ng/mL）
  manufacturer TEXT,                 -- 厂家
  lot TEXT NOT NULL,                 -- 批号
  level TEXT NOT NULL,               -- L/H
  target REAL NOT NULL,              -- 靶值
  sd REAL NOT NULL,                  -- SD
  expiry_date TEXT,                  -- 质控品保质期
  source TEXT DEFAULT '固定',        -- 固定/累计（V1.3 明确默认 = '固定'）
  operator TEXT,                     -- 操作人员
  pre_processor TEXT,                -- 前处理人员
  created_at TEXT,
  UNIQUE(project_id, name, lot, level),  -- 同项目+同指标+水平+批号唯一
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- 测定值表（V1.3 改：project_id → test_item_id）
CREATE TABLE IF NOT EXISTS measurements(
  id INTEGER PRIMARY KEY,
  test_item_id INTEGER NOT NULL,     -- V1.3 改：指向 test_items.id
  project_id INTEGER NOT NULL,       -- V1.3 新增冗余字段（加速 JOIN projects）
  lot TEXT NOT NULL,
  level TEXT NOT NULL,
  date TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  value REAL NOT NULL,
  status TEXT,                       -- 在控 / 警告(1_2s) / 失控 / 作废
  rules TEXT,                        -- JSON 数组
  operator TEXT,
  pre_processor TEXT,
  note TEXT,
  created_at TEXT,
  FOREIGN KEY(test_item_id) REFERENCES test_items(id) ON DELETE CASCADE,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- 失控记录表（V1.3 大改：test_item_id 可空 + 删 project_id NOT NULL）
CREATE TABLE IF NOT EXISTS out_of_control(
  id INTEGER PRIMARY KEY,
  measurement_id INTEGER,            -- 可空（孤立记录兜底）
  test_item_id INTEGER,              -- V1.3 新增：检测指标ID（可空，见问题 14）
  project_id INTEGER,                -- V1.3 改：去掉 NOT NULL（修复问题 14）
  lot TEXT NOT NULL,                 -- 冗余存批号（失控上下文核心，保留）
  level TEXT NOT NULL,               -- 冗余存水平（保留）
  triggered_date TEXT NOT NULL,      -- 冗余存触发日期（保留）
  test_item_name TEXT,               -- V1.3 新增：冗余存检测指标名（项目删后前端可显示）
  project_name TEXT,                 -- V1.3 新增：冗余存项目名（项目删后前端可显示）
  rules TEXT,
  error_type TEXT,
  checklist TEXT,                    -- JSON
  conclusion TEXT,
  operator TEXT,
  reviewed_by TEXT,
  sample_batch TEXT,
  sample_no TEXT,
  created_at TEXT,
  FOREIGN KEY(measurement_id) REFERENCES measurements(id) ON DELETE SET NULL
  -- V1.3 修复问题 14：test_item_id 和 project_id 都不加外键
  -- 因为删除检测指标/项目时需保留失控记录（医疗合规）
);

-- 系统设置表（V1.3 沿用）
CREATE TABLE IF NOT EXISTS lab_settings(
  id INTEGER PRIMARY KEY,
  lab_name TEXT
);
"""


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """打开连接并启用外键。

    注意：sqlite3 的 PRAGMA foreign_keys 是 per-connection 的，
    每次新连接都必须执行一次，否则 CASCADE / SET NULL 全部失效。
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_database(db_path: str | Path) -> None:
    """按 spec §8.1 原样建表（IF NOT EXISTS，重复执行安全）。"""
    # 顺序很重要：projects → test_items → measurements → out_of_control → lab_settings
    # 外键依赖的表必须先建

    # 数据源只有一个：SCHEMA_SQL。spec 怎么写，库就怎么建，杜绝手工抄写偏差。
    # 注意 SCHEMA_SQL 内含多语句（每条 DDL 以分号结尾），sqlite3 默认就允许 execute 多语句。
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()