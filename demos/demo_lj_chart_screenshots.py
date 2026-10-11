"""Issue #5 acceptance demo: render 6 LJ chart screenshots.

Scenarios (per user request):
  1) in_control
  2) warning (1_2s)
  3) out_of_control (1_3s)
  4) void (gray X)
  5) SD=0 (only center line)
  6) no data (placeholder)

Output: demos/lj_chart_<scenario>.png (and overwrites demos/lj_chart.png for
the in-control case so the repo's pre-existing file stays meaningful).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qc_tool.db import init_database, get_connection
from qc_tool import projects as P
from qc_tool import lj_chart as LJ


def _insert(conn, *, test_item_id, project_id, date, value,
            level, lot, status, rules=None, seq=0):
    cur = conn.execute(
        "INSERT INTO measurements("
        "  test_item_id, project_id, lot, level, date, seq, value,"
        "  status, rules"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (test_item_id, project_id, lot, level, date, seq, value,
         status, json.dumps(rules, ensure_ascii=False) if rules else None),
    )
    conn.commit()
    return int(cur.lastrowid)


def _new_conn(idx: int) -> sqlite3.Connection:
    db = ROOT / "demos" / f"_demo_lj_{idx}.db"
    if db.exists():
        try:
            db.unlink()
        except OSError:
            pass
    init_database(db)
    c = get_connection(db)
    c.execute("INSERT INTO lab_settings(id, lab_name) VALUES (1, 'Demo Lab')")
    c.commit()
    return c


def _seed_target(conn) -> tuple[int, int]:
    """Seed the canonical project + L test item; return (project_id, test_item_id)."""
    p = P.create_project(conn, name="Vitamin D", short="Vit-D",
                         unit="ng/mL", manufacturer="Roche")
    ti = P.create_test_item(
        conn, p.id, name="D2", lot="LOT1", level="L",
        target=100.0, sd=2.0, short="D2-S", unit="ng/mL",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    conn.commit()
    return p.id, ti.id


def _save(fig, name: str) -> Path:
    p = ROOT / "demos" / name
    fig.savefig(p, dpi=120, bbox_inches="tight")
    return p


def main() -> int:
    out_dir = ROOT / "demos"
    out_dir.mkdir(exist_ok=True)
    YEAR, MONTH = 2026, 4

    # ---- 1) in-control only ----
    c = _new_conn(1)
    pid, tiid = _seed_target(c)
    for d, v in [("2026-04-01", 99.5), ("2026-04-02", 100.4),
                 ("2026-04-03", 100.1), ("2026-04-04", 99.8),
                 ("2026-04-05", 100.6), ("2026-04-06", 99.9)]:
        _insert(c, test_item_id=tiid, project_id=pid, date=d, value=v,
                level="L", lot="LOT1", status="InControl", rules=[])
    data = LJ.compute_chart_data(c, project_id=pid, test_item_name="D2",
                                 lot="LOT1", level="L",
                                 year=YEAR, month=MONTH)
    fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
    out = _save(fig, "lj_chart_1_in_control.png")
    # also overwrite the repo's pre-existing file
    fig.savefig(ROOT / "demos" / "lj_chart.png", dpi=120, bbox_inches="tight")
    print(f"[1/6] in_control     -> {out.name} (+ lj_chart.png)")
    c.close()

    # ---- 2) warning (1_2s) ----
    c = _new_conn(2)
    pid, tiid = _seed_target(c)
    seq = 0
    for d, v in [("2026-04-01", 100.1), ("2026-04-02", 99.8),
                 ("2026-04-03", 100.4), ("2026-04-04", 100.2),
                 ("2026-04-05", 100.0), ("2026-04-06", 103.6)]:  # +1.8 SD
        status = "Warning(1_2s)" if v == 103.6 else "InControl"
        rules = ["1_2s"] if v == 103.6 else None
        _insert(c, test_item_id=tiid, project_id=pid, date=d, value=v,
                level="L", lot="LOT1", status=status, rules=rules, seq=seq)
        seq += 1
    data = LJ.compute_chart_data(c, project_id=pid, test_item_name="D2",
                                 lot="LOT1", level="L",
                                 year=YEAR, month=MONTH)
    fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
    out = _save(fig, "lj_chart_2_warning.png")
    print(f"[2/6] warning        -> {out.name}")
    c.close()

    # ---- 3) out-of-control (1_3s) ----
    c = _new_conn(3)
    pid, tiid = _seed_target(c)
    seq = 0
    for d, v in [("2026-04-01", 100.0), ("2026-04-02", 99.7),
                 ("2026-04-03", 100.3), ("2026-04-04", 99.9),
                 ("2026-04-05", 100.2), ("2026-04-06", 107.5)]:  # +3.75 SD
        status = "OutOfControl(1_3s)" if v == 107.5 else "InControl"
        rules = ["1_3s"] if v == 107.5 else None
        _insert(c, test_item_id=tiid, project_id=pid, date=d, value=v,
                level="L", lot="LOT1", status=status, rules=rules, seq=seq)
        seq += 1
    data = LJ.compute_chart_data(c, project_id=pid, test_item_name="D2",
                                 lot="LOT1", level="L",
                                 year=YEAR, month=MONTH)
    fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
    out = _save(fig, "lj_chart_3_out_of_control.png")
    print(f"[3/6] out_of_control -> {out.name}")
    c.close()

    # ---- 4) void (gray X) ----
    c = _new_conn(4)
    pid, tiid = _seed_target(c)
    seq = 0
    for d, v in [("2026-04-01", 100.0), ("2026-04-02", 99.8),
                 ("2026-04-03", 100.3), ("2026-04-04", 100.1),
                 ("2026-04-05", 99.9), ("2026-04-06", 105.0)]:
        status = "Void" if v == 105.0 else "InControl"
        _insert(c, test_item_id=tiid, project_id=pid, date=d, value=v,
                level="L", lot="LOT1", status=status, rules=None, seq=seq)
        seq += 1
    data = LJ.compute_chart_data(c, project_id=pid, test_item_name="D2",
                                 lot="LOT1", level="L",
                                 year=YEAR, month=MONTH)
    fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
    out = _save(fig, "lj_chart_4_void.png")
    print(f"[4/6] void           -> {out.name}")
    c.close()

    # ---- 5) SD=0 ----
    c = _new_conn(5)
    p = P.create_project(c, name="Vitamin D", short="Vit-D",
                         unit="ng/mL", manufacturer="Roche")
    # create_test_item rejects SD<=0; we bypass via direct SQL (demo only).
    cur = c.execute(
        "INSERT INTO test_items("
        "  project_id, name, lot, level, target, sd, short, unit,"
        "  manufacturer, operator, pre_processor"
        ") VALUES (?, 'D2', 'LOT1', 'L', 100.0, 0.0, 'D2-S', 'ng/mL',"
        "          'Roche', 'Op-1', 'Pre-1')",
        (p.id,),
    )
    tiid = int(cur.lastrowid)
    c.commit()
    pid = p.id
    for d, v in [("2026-04-01", 100.0), ("2026-04-02", 100.0),
                 ("2026-04-03", 100.0)]:
        _insert(c, test_item_id=tiid, project_id=pid, date=d, value=v,
                level="L", lot="LOT1", status="InControl", rules=[])
    data = LJ.compute_chart_data(c, project_id=pid, test_item_name="D2",
                                 lot="LOT1", level="L",
                                 year=YEAR, month=MONTH)
    fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
    out = _save(fig, "lj_chart_5_sd_zero.png")
    print(f"[5/6] SD=0           -> {out.name}")
    c.close()

    # ---- 6) no data ----
    c = _new_conn(6)
    pid = P.create_project(c, name="Empty Project", short="EP",
                           unit="", manufacturer="").id
    P.create_test_item(
        c, pid, name="D2", lot="LOT1", level="L",
        target=100.0, sd=2.0, short="D2-S", unit="ng/mL",
        manufacturer="Roche", operator="Op-1", pre_processor="Pre-1",
    )
    c.commit()
    data = LJ.compute_chart_data(c, project_id=pid, test_item_name="D2",
                                 lot="LOT1", level="L",
                                 year=YEAR, month=MONTH)
    fig = LJ.render_lj_chart(data, meta=LJ.make_default_meta())
    out = _save(fig, "lj_chart_6_no_data.png")
    print(f"[6/6] no_data        -> {out.name}")
    c.close()

    # clean up demo db files
    for i in range(1, 7):
        f = ROOT / "demos" / f"_demo_lj_{i}.db"
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
