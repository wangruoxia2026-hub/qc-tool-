"""
合并扫描按「同日期 + 同序号」配对判定（用户 2026-10-13 裁定）+ 2_2s① 标记两条。
测试先行：本文件先跑红，再实现 qc_tool/measurements.py 与 qc_tool/excel_io.py 的修复。

用户最终裁定的规则表（实现依据）：
- 1_3s        单值 > 3SD → 失控
- 1_2s        单值 > 2SD → 警告（独立标记）
- 2_2s ①     同水平连续 2 条同方向 > 2SD（保留跨日）→ 两条均失控
- 2_2s ②     同日同序号两水平同方向 > 2SD → 两条均失控
- R_4s 同水平线  同日同水平连续 2 条，一超 +2SD 一超 −2SD（正反两方向都算），
               极差 ≥ 4SD → 涉及的两条均失控；H/L 各自独立算
- R_4s 跨水平    同日同序号，一超 +2SD 一超 −2SD，极差 ≥ 4SD → 两条均失控
- 4_1s ①     同水平连续 4 条同方向 > 1SD（保留跨日）→ 第 4 条失控
- 4_1s ②     同日相邻 seq 两对，每对两水平同方向 > 1SD → 全部 4 个点失控
- 10_x ①     同水平连续 10 条同侧（跨日累计）→ 第 10 条失控
- 10_x ②     同日相邻 seq 连续 5 对，每对两水平同侧 → 全部 10 个点失控

"等于"控制限不算超出（取整到 3 位小数后严格 >）。
"""
import json
from pathlib import Path

import pytest

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import measurements as M
from qc_tool import excel_io as X


# 2026-10-13 事故数据（ALT 批号 20261001，17 对，靶值 L=10/SD=2，H=50/SD=6）
ALT_L = [
    7.92160873809815, 9.19857429057021, 9.92476163891927, 11.7420945382388,
    8.3019954056723, 12.6690088245178, 12.8031850750199, 11.5443522378924,
    7.98879459070519, 9.99926973168236, 9.66817561777867, 9.80267013757094,
    7.86755206587607, 11.4330111672839, 8.04460322706989, 16.0, 20.0,
]
ALT_H = [
    54.564747560716, 39.1471043329849, 44.035831007745, 50.3300997598865,
    47.456345532449, 62.2288430557061, 47.1499908979905, 38.6835979667273,
    36.3372361250718, 43.9432165179211, 49.000484275723, 40.0095836921705,
    36.0012504668706, 44.4268253292054, 51.9517338758759, 70.0, 75.0,
]


# ============ 固定装置：ALT L/H（同事故数据配置） ============

@pytest.fixture
def alt_db(tmp_path: Path):
    db_file = tmp_path / "qc_test.db"
    init_database(db_file)
    conn = get_connection(db_file)
    conn.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, '测试检验科')")
    p = P.create_project(conn, name="生化", short="BIO", unit="U/L", manufacturer="迈瑞")
    tl = P.create_test_item(
        conn, project_id=p.id, name="ALT", short="ALT", unit="U/L",
        lot="20261001", level="L", target=10.0, sd=2.0,
    )
    th = P.create_test_item(
        conn, project_id=p.id, name="ALT", short="ALT", unit="U/L",
        lot="20261001", level="H", target=50.0, sd=6.0,
    )
    conn.commit()
    return conn, p.id, tl.id, th.id


def add(conn, pid, tid, level, date, value, seq, lot="20261001", skip=True):
    """入库但不触发扫描（模拟批量导入），之后统一 rescan。"""
    return M.create_measurement(
        conn, test_item_id=tid, project_id=pid, lot=lot,
        level=level, date=date, value=value, seq=seq, _skip_rescan=skip,
    )


def rules_of(conn, mid) -> list:
    m = M.get_measurement(conn, mid)
    return json.loads(m.rules) if m.rules else []


def ooc_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM out_of_control").fetchone()[0]


# ============ 1. 事故数据回归：不得连坐 ============

class TestAltRealDataRegression:
    """2026-10-13 事故数据：同一天 17 对。

    按用户裁定规则表重判的预期：
    - 10_x②：L seq9–13（7.989/9.999/9.668/9.803/7.868）与 H 同序号 5 个点
      （36.34/43.94/49.00/40.01/36.00）都连续 5 对落在靶值下侧 → 10 个点全标；
    - 2_2s①/② + 4_1s②：L-16.0/L-20.0/H-70.0/H-75.0 共 4 条；
    - 警告 1 条：H-62.23（1_2s）；
    - 其余 19 条在控（不连坐）。
    """

    def test_no_blanket_marking(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        for i, (lv, hv) in enumerate(zip(ALT_L, ALT_H), start=1):
            add(conn, pid, tid_l, "L", "2026-10-02", lv, i)
            add(conn, pid, tid_h, "H", "2026-10-02", hv, i)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()

        rows = {}
        for tid in (tid_l, tid_h):
            for m in M.list_measurements(conn, tid, include_void=True):
                rows[m.id] = m
        ooc = [m for m in rows.values() if m.status and m.status.startswith("失控")]
        warn = [m for m in rows.values() if m.status and m.status.startswith("警告")]
        inctl = [m for m in rows.values() if m.status == "在控"]

        assert len(ooc) == 14, f"应只有 14 条失控，实际 {len(ooc)}"
        assert len(warn) == 1
        assert len(inctl) == 19

        def find(level, value):
            return next(
                m for m in rows.values()
                if m.level == level and abs(m.value - value) < 1e-9
            )

        # L-16.0：恰好 3SD → 无 1_3s（等于不算超出），同序号对(H-70.0)触发 2_2s②
        l16 = find("L", 16.0)
        assert json.loads(l16.rules) == ["1_2s", "2_2s", "4_1s"], l16.rules
        # L-20.0 / H-70.0 / H-75.0
        assert json.loads(find("L", 20.0).rules) == ["1_2s", "1_3s", "2_2s", "4_1s"]
        assert json.loads(find("H", 70.0).rules) == ["1_2s", "1_3s", "2_2s", "4_1s"]
        assert json.loads(find("H", 75.0).rules) == ["1_2s", "1_3s", "2_2s", "4_1s"]
        # 10_x② 标记的 10 条（两水平各连续 5 对同侧）
        for v in (7.98879459070519, 9.99926973168236, 9.66817561777867,
                  9.80267013757094, 7.86755206587607):
            m = find("L", v)
            assert json.loads(m.rules) == ["10_x"], (v, m.rules)
        for v in (43.9432165179211, 49.000484275723, 40.0095836921705):
            m = find("H", v)
            assert json.loads(m.rules) == ["10_x"], (v, m.rules)
        for v in (36.3372361250718, 36.0012504668706):
            m = find("H", v)
            assert json.loads(m.rules) == ["10_x", "1_2s"], (v, m.rules)
        # 警告 1 条：H-62.23
        m = find("H", 62.2288430557061)
        assert m.status.startswith("警告")
        assert json.loads(m.rules) == ["1_2s"]
        # 在控行不得带任何规则
        for m in inctl:
            assert m.rules is None, (m.value, m.rules)
        # OOC 表只有 14 条记录
        assert ooc_count(conn) == 14


# ============ 2. 跨水平 2_2s ②：同日期同序号配对 ============

class TestCrossLevel2_2s:
    def test_same_seq_marks_both(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 10.0, 1)
        b = add(conn, pid, tid_h, "H", "2026-10-02", 50.0, 1)
        c = add(conn, pid, tid_l, "L", "2026-10-02", 16.0, 3)
        d = add(conn, pid, tid_h, "H", "2026-10-02", 70.0, 3)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        assert M.get_measurement(conn, c.id).status.startswith("失控")
        assert "2_2s" in rules_of(conn, c.id)
        assert M.get_measurement(conn, d.id).status.startswith("失控")
        assert "2_2s" in rules_of(conn, d.id)
        # 在控的对不受连坐
        assert M.get_measurement(conn, a.id).status == "在控"
        assert M.get_measurement(conn, b.id).status == "在控"

    def test_different_seq_not_paired(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        c = add(conn, pid, tid_l, "L", "2026-10-02", 16.0, 3)
        d = add(conn, pid, tid_h, "H", "2026-10-02", 70.0, 4)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        # L 只有 1_2s 警告；H 只有 1_2s + 1_3s（个体规则），无 2_2s
        assert json.loads(M.get_measurement(conn, c.id).rules) == ["1_2s"]
        assert M.get_measurement(conn, c.id).status.startswith("警告")
        assert json.loads(M.get_measurement(conn, d.id).rules) == ["1_2s", "1_3s"]

    def test_same_seq_different_date_not_paired(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        c = add(conn, pid, tid_l, "L", "2026-10-02", 16.0, 1)
        d = add(conn, pid, tid_h, "H", "2026-10-03", 70.0, 1)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-03")
        conn.commit()
        assert "2_2s" not in rules_of(conn, c.id)
        assert "2_2s" not in rules_of(conn, d.id)


# ============ 3. R_4s 同水平线：同日连续两条一正一负 ============

class TestR4sSameLevel:
    def test_positive_then_negative(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 1)  # +2.005SD
        b = add(conn, pid, tid_l, "L", "2026-10-02", 5.99, 2)   # −2.005SD
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        assert json.loads(M.get_measurement(conn, a.id).rules) == ["1_2s", "R_4s"]
        assert json.loads(M.get_measurement(conn, b.id).rules) == ["1_2s", "R_4s"]

    def test_negative_then_positive(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 5.99, 1)
        b = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 2)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        assert "R_4s" in rules_of(conn, a.id)
        assert "R_4s" in rules_of(conn, b.id)

    def test_same_direction_not_trigger(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 1)
        b = add(conn, pid, tid_l, "L", "2026-10-02", 14.02, 2)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        # 同方向 → 不触发 R_4s（此处触发 2_2s① 两条）
        assert "R_4s" not in rules_of(conn, a.id)
        assert "R_4s" not in rules_of(conn, b.id)
        assert "2_2s" in rules_of(conn, a.id)
        assert "2_2s" in rules_of(conn, b.id)

    def test_one_side_within_2sd_not_trigger(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 1)  # +2.005SD
        b = add(conn, pid, tid_l, "L", "2026-10-02", 6.01, 2)   # −1.995SD 未超 2SD
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        assert "R_4s" not in rules_of(conn, a.id)
        assert M.get_measurement(conn, b.id).status == "在控"

    def test_cross_day_not_trigger(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 1)
        b = add(conn, pid, tid_l, "L", "2026-10-03", 5.99, 1)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-03")
        conn.commit()
        # R_4s 同水平线只算同日 → 跨日两条各自只是 1_2s 警告
        assert "R_4s" not in rules_of(conn, a.id)
        assert "R_4s" not in rules_of(conn, b.id)
        assert json.loads(M.get_measurement(conn, a.id).rules) == ["1_2s"]
        assert json.loads(M.get_measurement(conn, b.id).rules) == ["1_2s"]


# ============ 4. R_4s 跨水平：同日同序号一正一负 ============

class TestR4sCrossLevel:
    def test_same_seq_marks_both(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 2)  # +2.005SD
        b = add(conn, pid, tid_h, "H", "2026-10-02", 37.99, 2)  # −2.002SD
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        assert json.loads(M.get_measurement(conn, a.id).rules) == ["1_2s", "R_4s"]
        assert json.loads(M.get_measurement(conn, b.id).rules) == ["1_2s", "R_4s"]

    def test_different_seq_not_paired(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 2)
        b = add(conn, pid, tid_h, "H", "2026-10-02", 37.99, 3)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        assert "R_4s" not in rules_of(conn, a.id)
        assert "R_4s" not in rules_of(conn, b.id)


# ============ 5. 跨水平 4_1s ②：相邻两对 → 标 4 个点 ============

class TestCrossLevel4_1s:
    def test_marks_all_four(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        ids = [
            add(conn, pid, tid_l, "L", "2026-10-02", 12.1, 2).id,   # +1.05SD
            add(conn, pid, tid_h, "H", "2026-10-02", 56.5, 2).id,   # +1.083SD
            add(conn, pid, tid_l, "L", "2026-10-02", 12.2, 3).id,
            add(conn, pid, tid_h, "H", "2026-10-02", 56.6, 3).id,
        ]
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in ids:
            m = M.get_measurement(conn, mid)
            assert m.status.startswith("失控"), (m.level, m.value, m.status)
            assert json.loads(m.rules) == ["4_1s"], m.rules

    def test_mixed_direction_not_trigger(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        ids = [
            add(conn, pid, tid_l, "L", "2026-10-02", 12.1, 2).id,
            add(conn, pid, tid_h, "H", "2026-10-02", 43.7, 2).id,   # −1.05SD 反向
            add(conn, pid, tid_l, "L", "2026-10-02", 12.2, 3).id,
            add(conn, pid, tid_h, "H", "2026-10-02", 43.6, 3).id,
        ]
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in ids:
            assert "4_1s" not in rules_of(conn, mid)

    def test_seq_gap_breaks_continuity(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        ids = [
            add(conn, pid, tid_l, "L", "2026-10-02", 12.1, 1).id,
            add(conn, pid, tid_h, "H", "2026-10-02", 56.5, 1).id,
            add(conn, pid, tid_l, "L", "2026-10-02", 12.2, 3).id,   # seq 2 缺失
            add(conn, pid, tid_h, "H", "2026-10-02", 56.6, 3).id,
        ]
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in ids:
            assert "4_1s" not in rules_of(conn, mid)


# ============ 6. 跨水平 10_x ②：连续 5 对 → 标 10 个点 ============

class TestCrossLevel10_x:
    def test_marks_all_ten(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        ids = []
        for s in range(2, 7):
            ids.append(add(conn, pid, tid_l, "L", "2026-10-02", 10.5, s).id)  # +0.25SD
            ids.append(add(conn, pid, tid_h, "H", "2026-10-02", 50.5, s).id)  # +0.083SD
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in ids:
            m = M.get_measurement(conn, mid)
            assert m.status.startswith("失控"), (m.level, m.value, m.status)
            assert json.loads(m.rules) == ["10_x"], m.rules
        assert ooc_count(conn) == 10

    def test_broken_run_not_trigger(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        ids = []
        for s in range(2, 6):  # 4 对同侧
            ids.append(add(conn, pid, tid_l, "L", "2026-10-02", 10.5, s).id)
            ids.append(add(conn, pid, tid_h, "H", "2026-10-02", 50.5, s).id)
        # 第 5 对方向混合 → 断
        ids.append(add(conn, pid, tid_l, "L", "2026-10-02", 9.9, 6).id)
        ids.append(add(conn, pid, tid_h, "H", "2026-10-02", 50.5, 6).id)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in ids:
            assert "10_x" not in rules_of(conn, mid)
        assert ooc_count(conn) == 0

    def test_mixed_direction_not_trigger(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        ids = []
        for s in range(2, 7):
            ids.append(add(conn, pid, tid_l, "L", "2026-10-02", 10.5, s).id)  # 上侧
            ids.append(add(conn, pid, tid_h, "H", "2026-10-02", 49.5, s).id)  # 下侧
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in ids:
            assert "10_x" not in rules_of(conn, mid)

    def test_seq_gap_breaks_continuity(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        ids = []
        for s in (1, 2, 3, 4, 6):  # seq 5 缺失
            ids.append(add(conn, pid, tid_l, "L", "2026-10-02", 10.5, s).id)
            ids.append(add(conn, pid, tid_h, "H", "2026-10-02", 50.5, s).id)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in ids:
            assert "10_x" not in rules_of(conn, mid)


# ============ 7. 同水平 2_2s ①：标记两条均失控 ============

class TestSameLevel2_2sMarksBoth:
    def test_same_day_marks_both(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 1)
        b = add(conn, pid, tid_l, "L", "2026-10-02", 14.02, 2)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        for mid in (a.id, b.id):
            m = M.get_measurement(conn, mid)
            assert m.status.startswith("失控"), m.status
            assert "2_2s" in rules_of(conn, mid)

    def test_cross_day_retro_marks_earlier(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 1)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        assert M.get_measurement(conn, a.id).status.startswith("警告")  # 先只是警告
        b = add(conn, pid, tid_l, "L", "2026-10-03", 14.02, 1)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-03")
        conn.commit()
        # 两条都标 2_2s 失控，且早一天那条被回写 + 有 OOC 记录
        for mid in (a.id, b.id):
            m = M.get_measurement(conn, mid)
            assert m.status.startswith("失控"), m.status
            assert "2_2s" in rules_of(conn, mid)
        assert ooc_count(conn) == 2

    def test_forward_boundary_order_independent(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        a = add(conn, pid, tid_l, "L", "2026-10-02", 14.01, 1)
        b = add(conn, pid, tid_l, "L", "2026-10-03", 14.02, 1)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-03")
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-02")
        conn.commit()
        # 无论扫描顺序，两条都保持 2_2s 失控
        for mid in (a.id, b.id):
            assert "2_2s" in rules_of(conn, mid)
            assert M.get_measurement(conn, mid).status.startswith("失控")


# ============ 8. 跨日累计规则（保留） ============

class TestCrossDayRulesKept:
    def test_4_1s_cross_day_marks_fourth(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        rows = []
        for i, v in enumerate([12.05, 12.06, 12.07], start=1):  # +1.02x SD
            m = add(conn, pid, tid_l, "L", f"2026-10-0{i}", v, 1)
            M.rescan_day_for_test_item(conn, test_item_id=tid_l, date=f"2026-10-0{i}")
            rows.append(m)
        d4 = add(conn, pid, tid_l, "L", "2026-10-04", 12.08, 1)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-04")
        conn.commit()
        m4 = M.get_measurement(conn, d4.id)
        assert m4.status.startswith("失控")
        assert json.loads(m4.rules) == ["4_1s"]
        for m in rows:  # 前 3 条在控
            assert M.get_measurement(conn, m.id).status == "在控"

    def test_10_x_cross_day_marks_tenth(self, alt_db):
        conn, pid, tid_l, _ = alt_db
        rows = []
        for i in range(1, 10):
            m = add(conn, pid, tid_l, "L", f"2026-10-0{i}", 10.1, 1)  # +0.05SD
            M.rescan_day_for_test_item(conn, test_item_id=tid_l, date=f"2026-10-0{i}")
            rows.append(m)
        d10 = add(conn, pid, tid_l, "L", "2026-10-10", 10.2, 1)
        M.rescan_day_for_test_item(conn, test_item_id=tid_l, date="2026-10-10")
        conn.commit()
        m10 = M.get_measurement(conn, d10.id)
        assert m10.status.startswith("失控")
        assert json.loads(m10.rules) == ["10_x"]
        for m in rows:
            assert M.get_measurement(conn, m.id).status == "在控"


# ============ 9. 导入：seq 入库 + 重复导入拦截 ============

class TestImportSeqAndDedup:
    def _plan(self):
        plan = X.ImportPlan()
        plan.ok_rows = [
            X.ImportRow(project_name="生化", test_item_name="ALT", level="L",
                        lot="20261001", date="2026-04-01", value=10.1, seq=1),
            X.ImportRow(project_name="生化", test_item_name="ALT", level="L",
                        lot="20261001", date="2026-04-01", value=10.2, seq=2),
            X.ImportRow(project_name="生化", test_item_name="ALT", level="H",
                        lot="20261001", date="2026-04-01", value=50.1, seq=1),
            X.ImportRow(project_name="生化", test_item_name="ALT", level="H",
                        lot="20261001", date="2026-04-01", value=50.2, seq=2),
        ]
        return plan

    def _import(self, conn, pid, tid_l, tid_h, plan):
        return X.import_rows(
            conn, plan,
            project_resolver=lambda name: pid,
            test_item_resolver=lambda name, proj, level: tid_l if level == "L" else tid_h,
        )

    def test_seq_persisted_and_duplicate_rejected(self, alt_db):
        conn, pid, tid_l, tid_h = alt_db
        plan = self._plan()
        result = self._import(conn, pid, tid_l, tid_h, plan)
        conn.commit()
        assert result["imported_count"] == 4
        l_rows = M.list_measurements(conn, tid_l, include_void=True)
        h_rows = M.list_measurements(conn, tid_h, include_void=True)
        assert sorted(m.seq for m in l_rows) == [1, 2]
        assert sorted(m.seq for m in h_rows) == [1, 2]
        # 同文件再导一次 → 必须抛 DuplicateImportRows
        with pytest.raises(M.DuplicateImportRows):
            self._import(conn, pid, tid_l, tid_h, self._plan())
