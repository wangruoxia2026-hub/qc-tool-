"""
建库模块 —— spec V1.3 §8.1。

对外暴露：
- init_database(db_path): 创建/打开 SQLite 库并执行 §8.1 的五张表 SQL；
- get_connection(db_path): 返回启用了 PRAGMA foreign_keys=ON 的连接。

设计要点（AGENTS.md 铁律 + spec §8.3 a/b/c）：
1. 五张表 SQL 字符串逐字符照抄 §8.1，字段名/类型/UNIQUE/外键 ON DELETE 不许改；
2. CREATE TABLE 使用 IF NOT EXISTS，重复调用安全（幂等）；
3. PRAGMA foreign_keys 是 per-connection 行为，**每一次** get_connection 都必须
   显式设置（spec §8.1 注："注意启用 SQLite 的外键开关，否则 CASCADE 不生效"）；
4. 不在这里做任何业务逻辑（不写 Westgard 判定、不写导入导出），仅建库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# §8.1 表结构 SQL —— 严格按 spec 原文，不许改字段名/类型/UNIQUE/外键 ON DELETE
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  short TEXT,
  unit TEXT,
  manufacturer TEXT,
  created_at TEXT,
  UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS test_items(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  short TEXT,
  unit TEXT,
  manufacturer TEXT,
  lot TEXT NOT NULL,
  level TEXT NOT NULL,
  target REAL NOT NULL,
  sd REAL NOT NULL,
  expiry_date TEXT,
  source TEXT DEFAULT '固定',
  operator TEXT,
  pre_processor TEXT,
  created_at TEXT,
  UNIQUE(project_id, name, lot, level),
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS measurements(
  id INTEGER PRIMARY KEY,
  test_item_id INTEGER NOT NULL,
  project_id INTEGER NOT NULL,
  lot TEXT NOT NULL,
  level TEXT NOT NULL,
  date TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  value REAL NOT NULL,
  status TEXT,
  rules TEXT,
  operator TEXT,
  pre_processor TEXT,
  note TEXT,
  created_at TEXT,
  FOREIGN KEY(test_item_id) REFERENCES test_items(id) ON DELETE CASCADE,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS out_of_control(
  id INTEGER PRIMARY KEY,
  measurement_id INTEGER,
  test_item_id INTEGER,
  project_id INTEGER,
  lot TEXT NOT NULL,
  level TEXT NOT NULL,
  triggered_date TEXT NOT NULL,
  test_item_name TEXT,
  project_name TEXT,
  rules TEXT,
  error_type TEXT,
  checklist TEXT,
  conclusion TEXT,
  operator TEXT,
  reviewed_by TEXT,
  sample_batch TEXT,
  sample_no TEXT,
  created_at TEXT,
  FOREIGN KEY(measurement_id) REFERENCES measurements(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS lab_settings(
  id INTEGER PRIMARY KEY,
  lab_name TEXT
);
"""


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """
    返回一个 sqlite3 连接，row_factory=Row，每次自动启用外键。

    注意：PRAGMA foreign_keys 是 per-connection 的；连接关闭即失效，所以每个
    新连接都要重新设一次。详见 SQLite 文档 / spec §8.1。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_database(db_path: str | Path) -> None:
    """
    建库 + 建表。库文件不存在则创建；表存在则跳过（IF NOT EXISTS）。

    幂等：可重复调用，不会报错也不会丢表（spec §8.3 a 验收）。
    """
    db_path = Path(db_path)
    # 确保父目录存在（首次启动 backup 目录的兄弟目录也允许，但 db 文件所在目录
    # 一定要存在，否则 sqlite3.connect 会失败）
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()