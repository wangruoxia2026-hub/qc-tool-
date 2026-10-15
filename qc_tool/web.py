"""
Web 预览入口（spec V1.3 全 7 功能）

调用：
    python -m qc_tool.web
    # → http://127.0.0.1:8765/
    # 浏览器自动打开；同目录无 qc.db 则建库（spec §8.3 b）
    # 关闭浏览器后：state.json last_exit_clean=true（spec §8.2）

设计要点（AGENTS.md 铁律 1）：
1. 业务模块 0 修改 —— 所有 API 端点都 import qc_tool/{projects,measurements,
   lj_chart,reports,statistics,investigation,backup,db} 当后端；
2. 与 PyQt6 入口并行 —— ui_main.py + main_window_b.py 完全不动；
3. 端口冲突自动 +1 试下一个（不抛、不卡）；
4. 每次响应不缓存（no-cache headers）—— DB 改了 Ctrl+R 立刻看到。
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import traceback
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 确保 matplotlib 缓存目录位于项目本地，不触碰外部用户目录
if "MPLCONFIGDIR" not in os.environ:
    os.environ["MPLCONFIGDIR"] = str(Path(__file__).resolve().parent.parent / ".mpl_cache")
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from .db import get_connection, init_database
from . import projects as P
from . import measurements as M
from . import lj_chart as LJ
from . import reports as R
from . import statistics as S
from . import investigation as I
from . import release_advice as RA
from . import backup
from . import excel_io as X

# ---------------------------------------------------------------------------
# DB 管理（per-request 新连接，避免 sqlite 多线程问题）
# ---------------------------------------------------------------------------

class _DB:
    """线程局部 DB 连接。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = __import__("threading").local()

    def get(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            init_database(self.db_path)
            self._local.conn = get_connection(self.db_path)
        return self._local.conn


# ---------------------------------------------------------------------------
# 模板加载（HTML 写在 qc_tool/web/templates/index.html，与本文件同包）
# ---------------------------------------------------------------------------

def _load_template() -> bytes:
    p = Path(__file__).parent / "web" / "templates" / "index.html"
    return p.read_text(encoding="utf-8").encode("utf-8")


# ---------------------------------------------------------------------------
# JSON 工具
# ---------------------------------------------------------------------------

def _to_jsonable(obj: Any) -> Any:
    """dataclass / sqlite3.Row → dict。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, sqlite3.Row):
        return {k: obj[k] for k in obj.keys()}
    # dataclass
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return str(obj)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(_to_jsonable(payload), ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.end_headers()
    handler.wfile.write(body)


def _json_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _json_response(handler, status, {"error": message})


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    """读 JSON body。

    若 Content-Type 不是 application/json（如 multipart 上传走错路由），
    返回空 dict —— 路由不会匹配，最终落到 404 而不是 500。
    """
    ct = (handler.headers.get("Content-Type") or "").lower()
    if "json" not in ct:
        # 仍消耗 body（HTTP 协议要求读完），但不解析
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if length > 0:
            handler.rfile.read(length)
        return {}
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Handler 工厂
# ---------------------------------------------------------------------------

def make_handler(*, db_path: Path) -> type[BaseHTTPRequestHandler]:
    """返回绑定到 db_path 的 Handler 类。"""
    db = _DB(db_path)

    class Handler(BaseHTTPRequestHandler):
        # 抑制默认 access log（避免刷屏）
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")

        # ---- 工具 ----

        def _db(self) -> sqlite3.Connection:
            return db.get()

        # ---- GET ----

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._dispatch_get()
            except Exception as e:
                traceback.print_exc()
                _json_error(self, 500, f"{type(e).__name__}: {e}")

        def _dispatch_get(self) -> None:
            url = urlparse(self.path)
            path = url.path
            qs = parse_qs(url.query)

            # 根页面
            if path in ("/", "/index.html"):
                body = _load_template()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            # lab
            if path == "/api/lab":
                name = P.get_lab_name(self._db())
                _json_response(self, 200, {"lab_name": name})
                return

            # projects
            if path == "/api/projects":
                _json_response(
                    self, 200,
                    P.list_projects(self._db()),
                )
                return

            # test_items 列表（/api/projects/<pid>/test_items）
            if path.startswith("/api/projects/") and path.endswith("/test_items"):
                pid = int(path.split("/")[3])
                items = P.list_test_items(self._db(), pid)
                _json_response(self, 200, items)
                return

            # measurements 列表（/api/test_items/<tid>/measurements）
            if path.startswith("/api/test_items/") and path.endswith("/measurements"):
                tid = int(path.split("/")[3])
                rows = M.list_measurements(self._db(), tid, include_void=True)
                _json_response(self, 200, rows)
                return

            # 自定义区间统计（GET /api/stats/<tid>/custom?start=YYYY-MM-DD&end=YYYY-MM-DD&exclude_ooc=true/false）
            if path.startswith("/api/stats/") and path.endswith("/custom"):
                parts = path.split("/")
                # 路径结构 /api/stats/<tid>/custom，split 后为 ['', 'api', 'stats', '<tid>', 'custom']
                if len(parts) < 5 or not parts[3].isdigit():
                    _json_error(self, 404, f"bad custom stats path: {path}")
                    return
                tid = int(parts[3])
                ti = P.get_test_item(self._db(), tid)
                if ti is None:
                    _json_error(self, 404, "test_item not found")
                    return
                
                # 获取数据整体起止区间（供默认带入）
                db_min, db_max = S.get_data_date_range(self._db(), tid, ti.lot, ti.level)
                
                # 获取查询参数
                today = date.today()
                default_start = db_min or f"{today.year}-{today.month:02d}-01"
                default_end = db_max or today.isoformat()
                
                date_from = qs.get("start", [default_start])[0].strip() or default_start
                date_to = qs.get("end", [default_end])[0].strip() or default_end
                exclude_ooc_raw = qs.get("exclude_ooc", ["true"])[0].lower()
                exclude_ooc = exclude_ooc_raw in ("true", "1", "yes", "t")

                stats = S.compute_interval_stats(
                    self._db(),
                    test_item_id=tid,
                    lot=ti.lot,
                    level=ti.level,
                    date_from=date_from,
                    date_to=date_to,
                    exclude_ooc=exclude_ooc,
                )
                res = stats.to_dict()
                res["date_range"] = [date_from, date_to]
                res["db_range"] = [db_min, db_max]
                res["exclude_ooc"] = exclude_ooc
                _json_response(self, 200, res)
                return

            # 统计（/api/stats/<tid>?year=YYYY&month=M&source=固定|累计）
            if path.startswith("/api/stats/"):
                # 路径形如 /api/stats/123 → 123 可能带 ".something"，但我们这里只接数字
                parts = path.split("/")
                if len(parts) < 4 or not parts[3].isdigit():
                    _json_error(self, 404, f"bad stats path: {path}")
                    return
                tid = int(parts[3])
                today = date.today()
                year = int(qs.get("year", [today.year])[0])
                month = int(qs.get("month", [today.month])[0])
                source = qs.get("source", ["固定"])[0]
                ti = P.get_test_item(self._db(), tid)
                if ti is None:
                    _json_error(self, 404, "test_item not found")
                    return
                view = S.get_statistics_view(
                    self._db(), tid, ti.lot, ti.level,
                    source=source, today=date(year, month, 1),
                )
                _json_response(self, 200, view)
                return

            # LJ chart（/api/chart/<tid>.svg?year=YYYY&month=M&status=<filter>）
            if path.startswith("/api/chart/") and path.endswith(".svg"):
                # 路径形如 /api/chart/123.svg
                parts = path.split("/")
                if len(parts) < 4:
                    _json_error(self, 404, f"bad chart path: {path}")
                    return
                tid_str = parts[3].replace(".svg", "")
                if not tid_str.isdigit():
                    _json_error(self, 404, f"bad chart tid: {tid_str}")
                    return
                tid = int(tid_str)
                today = date.today()
                year = int(qs.get("year", [today.year])[0])
                month = int(qs.get("month", [today.month])[0])
                status_filter = qs.get("status", [None])[0]
                info_filter = qs.get("info", [None])[0]
                scope = qs.get("scope", [None])[0]
                self._render_chart_svg(tid, year, month, status_filter, info_filter, scope)
                return

            # §F.1 排查清单（用户需求 2026-10-14：点击图上点 → 右侧排查面板）
            # GET /api/investigation/plan?rules=1_3s,2_2s → spec 原文分类清单
            if path == "/api/investigation/plan":
                rules_raw = qs.get("rules", [""])[0].strip()
                try:
                    parsed = json.loads(rules_raw) if rules_raw.startswith("[") else None
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, list):
                    rules = [str(r).strip() for r in parsed if str(r).strip()]
                else:
                    rules = [r.strip() for r in rules_raw.split(",") if r.strip()]
                plan = I.build_investigation_plan(
                    rules=rules, triggered_date="", project_name="",
                    test_item_name="", lot="", level="", value=0.0,
                )
                _json_response(self, 200, {"categories": plan["categories"]})
                return

            # 该点已保存的排查记录（排查面板联动 F.1；2026-10-14 用户补充裁定）
            # GET /api/ooc/<mid>/investigation → {rules, checklist, conclusion}
            if path.startswith("/api/ooc/") and path.endswith("/investigation"):
                mid = int(path.split("/")[3])
                rec = I.get_ooc_checklist(self._db(), mid)
                _json_response(self, 200, rec or {
                    "measurement_id": mid, "rules": [],
                    "checklist": {}, "conclusion": None,
                })
                return

            # 结果发放建议（用户需求 2026-10-14；点击面板"数据点排查信息"下方）
            # GET /api/ooc/<mid>/release-advice → {measurement_id, advice: {...}|null}
            if path.startswith("/api/ooc/") and path.endswith("/release-advice"):
                mid_str = path.split("/")[3]
                if not mid_str.isdigit():
                    _json_error(self, 404, f"bad ooc id: {mid_str}")
                    return
                advice = RA.build_release_advice(self._db(), int(mid_str))
                _json_response(self, 200, {
                    "measurement_id": int(mid_str),
                    "advice": advice,
                })
                return

            # 月报 xlsx
            if path == "/api/export/monthly.xlsx":
                self._export_monthly(qs)
                return

            # 失控 xlsx
            if path == "/api/export/ooc.xlsx":
                self._export_ooc(qs)
                return

            # 失控 PDF
            if path == "/api/export/ooc.pdf":
                self._export_ooc_pdf(qs)
                return

            # 备份列表
            if path == "/api/backups":
                self._list_backups()
                return

            # 状态（debug）
            if path == "/api/health":
                _json_response(self, 200, {"ok": True, "db": str(db_path)})
                return

            # §B.3 下载模板
            if path == "/api/template.xlsx":
                xlsx = X.build_template_xlsx()
                self.send_response(200)
                self.send_header("Content-Type",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Length", str(len(xlsx)))
                self.send_header("Content-Disposition",
                                 'attachment; filename="qc_template.xlsx"')
                self.end_headers()
                self.wfile.write(xlsx)
                return

            _json_error(self, 404, f"no route: {path}")

        def _render_chart_svg(self, tid: int, year: int, month: int, status_filter: str | None, info_filter: str | None = None, scope: str | None = None) -> None:
            ti = P.get_test_item(self._db(), tid)
            if ti is None:
                _json_error(self, 404, "test_item not found")
                return
            proj = P.get_project(self._db(), ti.project_id)
            if proj is None:
                _json_error(self, 404, "project not found")
                return

            # scope=single：前端点击具体 L/H 水平子行时只画该水平一条曲线，
            # 跳过 L/H 配对；缺省维持原有双轴配对行为
            if scope == "single":
                item_l = item_h = None
            else:
                # 检查是否存在同名同批号的成对 (L, H) 水平
                all_siblings = P.list_test_items(self._db(), ti.project_id, name=ti.name)
                same_lot_items = [x for x in all_siblings if x.lot == ti.lot]
                item_l = next((x for x in same_lot_items if x.level == "L"), None)
                item_h = next((x for x in same_lot_items if x.level == "H"), None)
                if not (item_l and item_h):
                    # 容错：如果同批号未齐但该指标名下存在 L 和 H，进行配对
                    item_l = next((x for x in all_siblings if x.level == "L"), None)
                    item_h = next((x for x in all_siblings if x.level == "H"), None)

            # 如果同时具备 L 和 H，则一图双 y 轴渲染（左轴 L、右轴 H，控制限
            # 重合；某水平 SD=0 时该侧仅中心线 + 提示，不影响另一侧）
            if item_l and item_h:
                data_l = LJ.compute_chart_data(
                    self._db(),
                    project_id=item_l.project_id,
                    test_item_name=item_l.name,
                    lot=item_l.lot,
                    level="L",
                    year=year, month=month,
                )
                data_h = LJ.compute_chart_data(
                    self._db(),
                    project_id=item_h.project_id,
                    test_item_name=item_h.name,
                    lot=item_h.lot,
                    level="H",
                    year=year, month=month,
                )
                if status_filter:
                    data_l = self._filter_chart_data(data_l, status_filter)
                    data_h = self._filter_chart_data(data_h, status_filter)
                svg = self._dual_data_to_svg(data_l, item_l, data_h, item_h, info_filter=info_filter)
            else:
                data = LJ.compute_chart_data(
                    self._db(),
                    project_id=ti.project_id,
                    test_item_name=ti.name,
                    lot=ti.lot,
                    level=ti.level,
                    year=year, month=month,
                )
                if status_filter:
                    data = self._filter_chart_data(data, status_filter)
                svg = self._data_to_svg(data, ti, info_filter=info_filter)

            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(svg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(svg)

        def _filter_chart_data(self, data, status_filter: str):
            """按状态过滤点（status_filter ∈ all/in/warning/ooc/void）。"""
            from dataclasses import replace
            from qc_tool.lj_chart import _ChartStatus
            mapping = {
                "in": (_ChartStatus.IN_CONTROL,),
                "warning": (_ChartStatus.WARNING,),
                "ooc": (_ChartStatus.OUT_OF_CONTROL,),
                "void": (_ChartStatus.VOID,),
            }
            want = mapping.get(status_filter)
            if not want:
                return data
            new_points = [p for p in data.points if p.status in want]
            return replace(data, points=new_points)

        def _data_to_svg(self, data, ti, info_filter: str | None = None) -> bytes:
            """把 LJChartData 渲染成 SVG（不依赖 matplotlib，浏览器内交互）。"""
            ml, mr, mb = 70, 30, 60
            inner_w = 900 - ml - mr
            inner_h = 370  # 绘图区高度固定；标题多行时整体加高

            def escape_svg(text: str) -> str:
                return (text.replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                            .replace('"', "&quot;"))

            target = ti.target
            sd = ti.sd
            if sd <= 0:
                return (f'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="480">'
                        f'<text x="20" y="40" fill="red">SD=0，控制限无效</text></svg>').encode("utf-8")

            # 标题（新格式多行，用户需求 2026-10-13）
            visible_keys = set(info_filter.split(",")) if info_filter else set(LJ.ALL_FIELDS)
            meta = LJ.make_default_meta(visible_keys)
            title_lines = LJ._build_title_lines(data, meta)
            if not title_lines:
                title_lines = [f"{ti.name} · {ti.lot} · {ti.level} · {data.year}-{data.month:02d} LJ 图"]

            # 首行「项目名质控图」字号 ×2（用户需求 2026-10-13 第二次调整）
            first_h, line_h, title_y0 = 24, 16, 24
            rest_n = max(len(title_lines) - 1, 0)
            mt = title_y0 + first_h + line_h * rest_n + 6
            W, H = 900, mt + inner_h + mb

            # Y 范围 = ±4SD
            y_min, y_max = target - 4 * sd, target + 4 * sd
            y_range = y_max - y_min or 1

            def y_to_px(v):
                return mt + inner_h * (1 - (v - y_min) / y_range)

            # 作废点不显示（spec §E.1），且不参与 X 轴占位
            pts = [p for p in data.points
                   if (p.status.value if hasattr(p.status, "value") else str(p.status)) != "void"]
            if pts:
                xs = list(range(len(pts)))
                def x_to_px(i):
                    if len(xs) <= 1:
                        return ml + inner_w / 2
                    return ml + inner_w * i / (len(xs) - 1)
            else:
                xs = []
                def x_to_px(i):
                    return ml

            out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                   f'viewBox="0 0 {W} {H}" font-family="Microsoft YaHei, sans-serif" font-size="12">']

            # 背景
            out.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#fafafa"/>')

            # 标题：逐行渲染（首行 ×2 字号）
            for i, line in enumerate(title_lines):
                y = title_y0 if i == 0 else title_y0 + first_h + (i - 1) * line_h
                size = 24 if i == 0 else 12
                weight = ' font-weight="bold"' if i == 0 else ""
                out.append(f'<text x="{W/2}" y="{y}" text-anchor="middle" font-size="{size}"{weight}>'
                           f'{escape_svg(line)}</text>')

            # ±4SD 范围内白底绘图区
            out.append(f'<rect x="{ml}" y="{mt}" width="{inner_w}" height="{inner_h}" fill="white" stroke="#ccc"/>')

            # 控制限线（±3, ±2, ±1, 0）—— spec §E.1：中心绿实线、±1SD 绿虚线、
            # ±2SD 黄虚线、±3SD 红实线（2026-10-12 用户确认对齐）
            for n, color, dash in [(-3, "#c0392b", ""), (-2, "#d9a400", "4,2"),
                                    (2, "#d9a400", "4,2"), (3, "#c0392b", ""),
                                    (-1, "#1f7a1f", "2,2"), (1, "#1f7a1f", "2,2"),
                                    (0, "#1f7a1f", "")]:
                v = target + n * sd
                if y_min <= v <= y_max:
                    y = y_to_px(v)
                    out.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+inner_w}" y2="{y:.1f}" '
                               f'stroke="{color}" stroke-width="1" stroke-dasharray="{dash}"/>')
                    out.append(f'<text x="{ml+inner_w+4}" y="{y+4:.1f}" fill="{color}" font-size="10">'
                               f'{"+" if n>0 else ""}{n}SD</text>')

            # 靶值文字
            out.append(f'<text x="{ml-6}" y="{y_to_px(target)+4:.1f}" text-anchor="end" fill="#1f7a1f" font-size="10">'
                       f'{target:.2f}</text>')

            # 点（spec §E.1：在控=黑色圆点、警告=黄色三角、失控=红色实心圆）
            color_map = {
                "in_control": "black",
                "warning": "#d9a400",
                "out_of_control": "#c0392b",
            }
            if not pts:
                out.append(f'<text x="{ml+inner_w/2}" y="{mt+inner_h/2}" text-anchor="middle" fill="#94a3b8" font-size="13">'
                           f'当前月份（{data.year}年{data.month}月）暂无测量数据，可切换月份或导入测定数据</text>')
            prev_xy = None
            for i, p in enumerate(pts):
                st_val = p.status.value if hasattr(p.status, "value") else str(p.status)
                v = target + p.sd_score * sd
                # 超出 ±4SD 的点不丢弃：钉在图区上/下边缘并标注实际值（悬停仍为真实数据）
                clamped = min(max(v, y_min), y_max)
                x = x_to_px(i)
                y = y_to_px(clamped)
                color = color_map.get(st_val, "black")
                # 折线（经过钉边位置，保持序列连续）
                if prev_xy is not None:
                    out.append(f'<line x1="{prev_xy[0]:.1f}" y1="{prev_xy[1]:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                               f'stroke="#0284c7" stroke-width="1.6" opacity="0.85"/>')
                prev_xy = (x, y)
                if v > y_max:
                    out.append(f'<text x="{x:.1f}" y="{mt+16:.1f}" text-anchor="middle" fill="{color}" font-size="10">'
                               f'↑{p.value:.3f}</text>')
                    y = mt + 3
                elif v < y_min:
                    out.append(f'<text x="{x:.1f}" y="{mt+inner_h-12:.1f}" text-anchor="middle" fill="{color}" font-size="10">'
                               f'↓{p.value:.3f}</text>')
                    y = mt + inner_h - 3
                # 点可点击（用户需求 2026-10-14：点击图上点 → 右侧排查面板）：
                # 外包 <g data-*> 供前端事件委托读取，再叠一层透明大热区便于点中
                if st_val == "out_of_control":
                    tip = (f"{p.date} seq={p.seq} value={p.value:.3f} "
                           f"sd={p.sd_score:+.2f} rules={','.join(p.rules) or '—'}")
                else:
                    tip = f"{p.date} seq={p.seq} value={p.value:.3f} sd={p.sd_score:+.2f}"
                pt_attrs = (f'data-mid="{p.measurement_id}" data-date="{p.date}" data-seq="{p.seq}" '
                            f'data-value="{p.value:.3f}" data-status="{st_val}" '
                            f'data-rules="{",".join(p.rules)}" data-level="{ti.level}"')
                out.append(f'<g class="pt" {pt_attrs} style="cursor:pointer">')
                # 透明热区垫底（先画）：放大点击命中半径；形状在上，悬停 tooltip 仍走形状
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="transparent" '
                           f'pointer-events="all"/>')
                if st_val == "out_of_control":
                    out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" stroke="#000" stroke-width="0.8">'
                               f'<title>{tip}</title></circle>')
                elif st_val == "warning":
                    out.append(f'<polygon points="{x:.1f},{y-7:.1f} {x-7:.1f},{y+5:.1f} {x+7:.1f},{y+5:.1f}" '
                               f'fill="{color}" stroke="#000" stroke-width="0.5">'
                               f'<title>{tip}</title></polygon>')
                else:
                    out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" stroke="#000" stroke-width="0.5">'
                               f'<title>{tip}</title></circle>')
                out.append('</g>')

            # X 轴：日期标签（每第 N 个）
            if pts:
                step = max(1, len(pts) // 8)
                for i in range(0, len(pts), step):
                    x = x_to_px(i)
                    out.append(f'<text x="{x:.1f}" y="{H-mb+15}" text-anchor="middle" font-size="10">{pts[i].date[5:]}</text>')

            # 图例
            lx = ml
            ly = H - 20
            for label, color, shape in [
                ("在控", "black", "circle"),
                ("警告", "#d9a400", "triangle"),
                ("失控", "#c0392b", "circle"),
            ]:
                if shape == "triangle":
                    out.append(f'<polygon points="{lx+5},{ly-6} {lx-2},{ly+4} {lx+12},{ly+4}" fill="{color}"/>')
                else:
                    out.append(f'<circle cx="{lx+5}" cy="{ly}" r="4" fill="{color}"/>')
                out.append(f'<text x="{lx+18}" y="{ly+4}" font-size="10">{label}</text>')
                lx += 70

            out.append('</svg>')
            return "".join(out).encode("utf-8")

        def _dual_data_to_svg(self, data_l, ti_l, data_h, ti_h, info_filter: str | None = None) -> bytes:
            """把高低双水平渲染成【一图双 y 轴】（左轴 L、右轴 H，控制限重合）。

            用户需求（2026-10-12 第二次补充，取代并排双图；spec §10 第 14 条
            经用户拍板开放）：
            - 左 y 轴 = L 水平测定值，右 y 轴 = H 水平测定值；
            - 两侧各 7 条控制限（靶值绿实线、±1SD 绿虚线、±2SD 黄虚线、
              ±3SD 红实线），左右像素级重合；
            - L 点：在控绿实心圆 / 警告黄实心圆 / 失控红实心圆；
              H 点：在控蓝实心三角 / 警告黄实心三角 / 失控红实心三角。
            """
            ml, mr, mb = 78, 78, 62
            inner_w = 960 - ml - mr
            inner_h = 418  # 绘图区高度固定；标题多行时整体加高

            def escape_svg(text: str) -> str:
                return (text.replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                            .replace('"', "&quot;"))

            # 标题（新格式多行，用户需求 2026-10-13）
            visible_keys = set(info_filter.split(",")) if info_filter else set(LJ.ALL_FIELDS)
            meta = LJ.make_default_meta(visible_keys)
            title_lines = LJ._dual_title_lines(data_l, data_h, meta)
            if not title_lines:
                title_lines = [f"{ti_l.name} · {ti_l.lot} · {data_l.year}-{data_l.month:02d} LJ 图"]

            # 首行「项目名质控图」字号 ×2（用户需求 2026-10-13 第二次调整）
            first_h, line_h, title_y0 = 24, 16, 24
            rest_n = max(len(title_lines) - 1, 0)
            # 标题区 = 标题行 + 一行水平说明（L 左轴 / H 右轴）
            mt = title_y0 + first_h + line_h * (rest_n + 1) + 6
            W, H = 960, mt + inner_h + mb

            # 双轴图点样式（L 圆 / H 三角；在控绿(L)/蓝(H)，警告黄，失控红）
            point_style = {
                "L": {"in_control": ("circle", "#1f7a1f"),
                      "warning": ("circle", "#d9a400"),
                      "out_of_control": ("circle", "#c0392b")},
                "H": {"in_control": ("triangle", "#1f5fa3"),
                      "warning": ("triangle", "#d9a400"),
                      "out_of_control": ("triangle", "#c0392b")},
            }

            def span_sd(data, ti):
                """该水平的 SD 跨度（下限 3.5）；SD<=0 或无点时返回 None。"""
                if ti.sd <= 0 or not data.points:
                    return None
                k = max(abs(p.sd_score) for p in data.points
                        if p.sd_score == p.sd_score)
                return max(3.5, k + 0.5)

            # 重合实现：两水平共用同一像素绘图区，各自按"靶值 ± M×本水平 SD"
            # 对称映射（M 取两侧跨度最大值）→ 同一 k·SD 线在左右两轴重合。
            m_l = span_sd(data_l, ti_l)
            m_h = span_sd(data_h, ti_h)
            # 可见跨度封顶 ±4SD（用户需求 2026-10-13）：极端离群点不压缩
            # 控制限；超出 ±4SD 的点钉在上下边缘 + 标注实际值
            M = min(max(m_l or 3.5, m_h or 3.5), 4.0)
            ref_l = ti_l.sd if ti_l.sd > 0 else (ti_h.sd if ti_h.sd > 0 else 1.0)
            ref_h = ti_h.sd if ti_h.sd > 0 else (ti_l.sd if ti_l.sd > 0 else 1.0)

            def y_to_px(v, target, ref_sd):
                y_min = target - M * ref_sd
                y_max = target + M * ref_sd
                return mt + inner_h * (1 - (v - y_min) / (y_max - y_min))

            # 作废点不显示（spec §E.1），不参与 X 轴占位
            def active(pts):
                return [p for p in pts
                        if (p.status.value if hasattr(p.status, "value")
                            else str(p.status)) != "void"]

            pts_l, pts_h = active(data_l.points), active(data_h.points)
            merged = sorted(
                [(p, "L") for p in pts_l] + [(p, "H") for p in pts_h],
                key=lambda t: (t[0].date, t[0].seq, t[0].measurement_id),
            )

            # 横轴：同日 L/H 逐点配对同 x（2026-10-13 用户第四次调整）
            # 每日期一组横位：该日 L、H 各自按（seq, measurement_id）排序后，
            # 第 i 个 L 点与第 i 个 H 点共用第 i 个横位；日期刻度居该组中点。
            lpts_by_date: dict[str, list] = {}
            hpts_by_date: dict[str, list] = {}
            for p in pts_l:
                lpts_by_date.setdefault(p.date, []).append(p)
            for p in pts_h:
                hpts_by_date.setdefault(p.date, []).append(p)
            for pts in list(lpts_by_date.values()) + list(hpts_by_date.values()):
                pts.sort(key=lambda p: (p.seq, p.measurement_id))
            dates = sorted(set(lpts_by_date) | set(hpts_by_date))
            slot_by_id: dict[int, int] = {}
            date_center: dict[str, float] = {}
            pos = 0
            for d in dates:
                lps = lpts_by_date.get(d, [])
                hps = hpts_by_date.get(d, [])
                n = max(len(lps), len(hps))
                for i, p in enumerate(lps):
                    slot_by_id[p.measurement_id] = pos + i
                for i, p in enumerate(hps):
                    slot_by_id[p.measurement_id] = pos + i
                date_center[d] = pos + (n - 1) / 2
                pos += n
            total_slots = pos

            def x_to_px(slot):
                if total_slots <= 1:
                    return ml + inner_w / 2
                return ml + inner_w * slot / (total_slots - 1)

            out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                   f'viewBox="0 0 {W} {H}" font-family="Microsoft YaHei, sans-serif" font-size="12">']
            out.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#fafafa"/>')

            # 标题：逐行渲染（首行 ×2 字号）+ 水平说明
            for i, line in enumerate(title_lines):
                y = title_y0 if i == 0 else title_y0 + first_h + (i - 1) * line_h
                size = 24 if i == 0 else 12
                weight = ' font-weight="bold"' if i == 0 else ""
                out.append(f'<text x="{W/2}" y="{y}" text-anchor="middle" font-size="{size}"{weight}>'
                           f'{escape_svg(line)}</text>')
            out.append(f'<text x="{W/2}" y="{title_y0 + first_h + rest_n * line_h}" text-anchor="middle" font-size="11" fill="#475569">'
                       f'低水平 L（左轴） · 高水平 H（右轴）</text>')

            # 绘图区白底
            out.append(f'<rect x="{ml}" y="{mt}" width="{inner_w}" height="{inner_h}" fill="white" stroke="#ccc"/>')

            if not merged:
                out.append(f'<text x="{ml+inner_w/2}" y="{mt+inner_h/2}" text-anchor="middle" '
                           f'fill="#94a3b8" font-size="13">'
                           f'当前月份（{data_l.year}年{data_l.month}月）暂无测量数据，可切换月份或导入测定数据</text>')

            # 控制限：每侧各画 7 条（同一 k·SD 线在左右两轴像素重合）；
            # 左列刻度 = L 测定值，右列刻度 = H 测定值；SD=0 侧仅中心线 + 提示
            for target, sd, ref_sd, side in (
                (ti_l.target, ti_l.sd, ref_l, "L"),
                (ti_h.target, ti_h.sd, ref_h, "H"),
            ):
                if sd <= 0:
                    y = y_to_px(target, target, ref_sd)
                    out.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+inner_w}" y2="{y:.1f}" '
                               f'stroke="#1f7a1f" stroke-width="1.1"/>')
                    out.append(f'<text x="{W/2}" y="{mt+inner_h-6:.1f}" text-anchor="middle" '
                               f'fill="red" font-size="11">{side} 水平 SD=0，控制限无效（仅中心线）</text>')
                    continue
                for n, color, dash, lbl in [
                    (-3, "#c0392b", "", "-3SD"),
                    (-2, "#d9a400", "4,2", "-2SD"),
                    (-1, "#1f7a1f", "2,2", "-1SD"),
                    (0, "#1f7a1f", "", "靶值"),
                    (1, "#1f7a1f", "2,2", "+1SD"),
                    (2, "#d9a400", "4,2", "+2SD"),
                    (3, "#c0392b", "", "+3SD"),
                ]:
                    v = target + n * sd
                    y = y_to_px(v, target, ref_sd)
                    out.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+inner_w}" y2="{y:.1f}" '
                               f'stroke="{color}" stroke-width="1.1" stroke-dasharray="{dash}"/>')
                    if side == "L":
                        out.append(f'<text x="{ml-6}" y="{y+4:.1f}" text-anchor="end" fill="{color}" font-size="10">'
                                   f'{v:.2f}（{lbl}）</text>')
                    else:
                        out.append(f'<text x="{ml+inner_w+6}" y="{y+4:.1f}" fill="{color}" font-size="10">'
                                   f'{v:.2f}（{lbl}）</text>')

            # 轴标题
            out.append(f'<text x="{ml-6}" y="{mt-12}" text-anchor="end" font-size="11" '
                       f'font-weight="bold" fill="#1f7a1f">L 测定值（左轴）</text>')
            out.append(f'<text x="{ml+inner_w+6}" y="{mt-12}" font-size="11" '
                       f'font-weight="bold" fill="#1f5fa3">H 测定值（右轴）</text>')

            # 点：L 圆 / H 三角，六种状态样式；同水平点之间浅灰连线；
            # 超 ±4SD 的点钉在上下边缘 + 标注实际值（悬停仍为真实数据）
            prev_by_level: dict[str, tuple[float, float]] = {}
            for i, (p, lvl) in enumerate(merged):
                st_val = p.status.value if hasattr(p.status, "value") else str(p.status)
                shape, color = point_style[lvl].get(st_val, point_style[lvl]["in_control"])
                x = x_to_px(slot_by_id[p.measurement_id])
                target_side = ti_l.target if lvl == "L" else ti_h.target
                ref_side = ref_l if lvl == "L" else ref_h
                sd_side = ti_l.sd if lvl == "L" else ti_h.sd
                y = y_to_px(p.value, target_side, ref_side)
                pin = ""
                if sd_side > 0:
                    if p.value > target_side + 4.0 * sd_side:
                        pin, y = "↑", mt + 3
                    elif p.value < target_side - 4.0 * sd_side:
                        pin, y = "↓", mt + inner_h - 3
                y = min(max(y, mt + 2), mt + inner_h - 2)  # 防退化场景越界
                if lvl in prev_by_level:
                    px, py = prev_by_level[lvl]
                    out.append(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                               f'stroke="#94a3b8" stroke-width="1.2" opacity="0.6"/>')
                prev_by_level[lvl] = (x, y)
                if pin:
                    y_annot = mt + 18 if pin == "↑" else mt + inner_h - 12
                    out.append(f'<text x="{x:.1f}" y="{y_annot:.1f}" text-anchor="middle" '
                               f'fill="{color}" font-size="10">{pin}{p.value:.3f}</text>')
                tip = (f"[{lvl}水平] {p.date} seq={p.seq}\n测定值: {p.value:.3f}\n"
                       f"SD偏离: {p.sd_score:+.2f}SD\n状态: {st_val}\n"
                       f"触发规则: {','.join(p.rules) or '无'}")
                # 点可点击（用户需求 2026-10-14：点击图上点 → 右侧排查面板）
                pt_attrs = (f'data-mid="{p.measurement_id}" data-date="{p.date}" data-seq="{p.seq}" '
                            f'data-value="{p.value:.3f}" data-status="{st_val}" '
                            f'data-rules="{",".join(p.rules)}" data-level="{lvl}"')
                out.append(f'<g class="pt" {pt_attrs} style="cursor:pointer">')
                # 透明热区垫底（先画）：放大点击命中半径；形状在上，悬停 tooltip 仍走形状
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="transparent" '
                           f'pointer-events="all"/>')
                if shape == "triangle":
                    out.append(f'<polygon points="{x:.1f},{y-7:.1f} {x-7:.1f},{y+5:.1f} {x+7:.1f},{y+5:.1f}" '
                               f'fill="{color}" stroke="#000" stroke-width="0.6">'
                               f'<title>{escape_svg(tip)}</title></polygon>')
                else:
                    out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" '
                               f'stroke="#000" stroke-width="0.6">'
                               f'<title>{escape_svg(tip)}</title></circle>')
                out.append('</g>')

            # X 轴日期刻度（每日期一组横位，标签居该组中点）
            step = max(1, len(dates) // 8)
            for d in dates[::step]:
                out.append(f'<text x="{x_to_px(date_center[d]):.1f}" y="{H-mb+16:.1f}" text-anchor="middle" '
                           f'font-size="10" fill="#475569">{d[5:]}</text>')

            # 图例：4 条控制限 + 六种点样式
            lx, ly = ml - 8, H - 16
            for shape, color, dash, label in [
                ("line", "#1f7a1f", "", "靶值"),
                ("line", "#1f7a1f", "2,2", "±1SD"),
                ("line", "#d9a400", "4,2", "±2SD"),
                ("line", "#c0392b", "", "±3SD"),
                ("circle", "#1f7a1f", "", "L 在控"),
                ("circle", "#d9a400", "", "L 警告"),
                ("circle", "#c0392b", "", "L 失控"),
                ("triangle", "#1f5fa3", "", "H 在控"),
                ("triangle", "#d9a400", "", "H 警告"),
                ("triangle", "#c0392b", "", "H 失控"),
            ]:
                if shape == "line":
                    out.append(f'<line x1="{lx}" y1="{ly}" x2="{lx+18}" y2="{ly}" '
                               f'stroke="{color}" stroke-width="1.5" stroke-dasharray="{dash}"/>')
                elif shape == "triangle":
                    out.append(f'<polygon points="{lx+9},{ly-6} {lx+2},{ly+4} {lx+16},{ly+4}" fill="{color}"/>')
                else:
                    out.append(f'<circle cx="{lx+9}" cy="{ly}" r="4.5" fill="{color}"/>')
                out.append(f'<text x="{lx+22}" y="{ly+4}" font-size="9" fill="#475569">{label}</text>')
                lx += 88

            out.append('</svg>')
            return "".join(out).encode("utf-8")

        def _export_monthly(self, qs: dict) -> None:
            try:
                pid = int(qs["project_id"][0])
                year = int(qs["year"][0])
                month = int(qs["month"][0])
            except (KeyError, ValueError, IndexError):
                _json_error(self, 400, "需要 project_id / year / month")
                return
            proj = P.get_project(self._db(), pid)
            if proj is None:
                _json_error(self, 404, "project not found")
                return
            items = P.list_test_items(self._db(), pid)
            if "selected" in qs and qs["selected"][0].strip():
                try:
                    sel_ids = [int(x.strip()) for x in qs["selected"][0].split(",") if x.strip()]
                except ValueError:
                    _json_error(self, 400, "selected 必须为以逗号分隔的数字 ID")
                    return
            else:
                sel_ids = [it.id for it in items]
            try:
                data = R.build_monthly_report_xlsx(
                    self._db(),
                    project_name=proj.name,
                    year=year, month=month,
                    selected_test_item_ids=sel_ids,
                )
            except R.NoIndicatorSelected:
                _json_error(self, 400, "未勾选任何指标")
                return
            self._send_xlsx(data, f"monthly_{proj.name}_{year}{month:02d}.xlsx")

        def _export_ooc(self, qs: dict) -> None:
            try:
                pid = int(qs["project_id"][0])
                df = qs["date_from"][0]
                dt = qs["date_to"][0]
            except (KeyError, ValueError, IndexError):
                _json_error(self, 400, "需要 project_id / date_from / date_to")
                return
            proj = P.get_project(self._db(), pid)
            if proj is None:
                _json_error(self, 404, "project not found")
                return
            data = R.build_ooc_report_xlsx(
                self._db(),
                project_name=proj.name,
                date_from=df, date_to_inclusive=dt,
            )
            self._send_xlsx(data, f"ooc_{proj.name}_{df}_{dt}.xlsx")

        def _export_ooc_pdf(self, qs: dict) -> None:
            try:
                pid = int(qs["project_id"][0])
                df = qs["date_from"][0]
                dt = qs["date_to"][0]
            except (KeyError, ValueError, IndexError):
                _json_error(self, 400, "需要 project_id / date_from / date_to")
                return
            proj = P.get_project(self._db(), pid)
            if proj is None:
                _json_error(self, 404, "project not found")
                return
            data = R.build_ooc_report_pdf(
                self._db(),
                project_name=proj.name,
                date_from=df, date_to_inclusive=dt,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="ooc_{proj.name}_{df}_{dt}.pdf"')
            self.end_headers()
            self.wfile.write(data)

        def _send_xlsx(self, data: bytes, filename: str) -> None:
            from urllib.parse import quote
            safe_filename = quote(filename)
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{safe_filename}; filename=\"export.xlsx\"")
            self.end_headers()
            self.wfile.write(data)

        def _list_backups(self) -> None:
            from .backup import BACKUP_DIRNAME
            bdir = db_path.parent / BACKUP_DIRNAME
            if not bdir.exists():
                _json_response(self, 200, [])
                return
            items = []
            for p in sorted(bdir.iterdir(), reverse=True):
                if p.is_file() and p.suffix == ".db":
                    items.append({
                        "name": p.name,
                        "size": p.stat().st_size,
                        "mtime": p.stat().st_mtime,
                    })
            _json_response(self, 200, items)

        # ---- POST ----

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._dispatch_post()
            except Exception as e:
                traceback.print_exc()
                _json_error(self, 500, f"{type(e).__name__}: {e}")

        def _dispatch_post(self) -> None:
            url = urlparse(self.path)
            path = url.path

            # §B.2 批量导入：用 raw bytes（xlsx），不走 JSON 解析
            if path.startswith("/api/test_items/") and path.endswith("/import"):
                self._post_import_xlsx()
                return

            # 数据导入预览（解析 xlsx 但不落库）
            if path.startswith("/api/test_items/") and path.endswith("/preview_import"):
                self._post_preview_import_xlsx()
                return

            # 上传 Excel 自动测算靶值与 SD
            if path == "/api/calculate_target_sd":
                self._post_calc_target_sd()
                return

            # §F.1 失控排查
            if path.startswith("/api/ooc/") and path.endswith("/investigate"):
                mid = int(path.split("/")[3])
                self._post_save_investigation(mid)
                return

            # §8.2 从备份恢复
            if path.startswith("/api/backups/") and path.endswith("/restore"):
                bname = path.split("/")[3]
                self._restore_backup(bname)
                return

            data = _read_json(self)

            # lab
            if path == "/api/lab":
                name = (data.get("lab_name") or "").strip()
                try:
                    P.set_lab_name(self._db(), name)
                    self._db().commit()
                except P.LabNameEmpty as e:
                    _json_error(self, 400, str(e))
                    return
                _json_response(self, 200, {"lab_name": P.get_lab_name(self._db())})
                return

            # projects
            if path == "/api/projects":
                try:
                    p = P.create_project(self._db(), **{
                        k: data.get(k) for k in ("name", "short", "unit", "manufacturer")
                    })
                    self._db().commit()
                    _json_response(self, 200, p)
                except P.ProjectNameEmpty as e:
                    _json_error(self, 400, str(e))
                except P.ProjectNameExists as e:
                    _json_error(self, 400, str(e))
                return

            # test_items
            if path.startswith("/api/projects/") and path.endswith("/test_items"):
                pid = int(path.split("/")[3])
                try:
                    kwargs = {
                        "name": data.get("name"),
                        "lot": data.get("lot"),
                        "level": data.get("level"),
                        "target": data.get("target"),
                        "sd": data.get("sd"),
                    }
                    for k in ("short", "unit", "manufacturer", "expiry_date", "operator", "pre_processor"):
                        if data.get(k) is not None:
                            kwargs[k] = data.get(k)
                    ti = P.create_test_item(self._db(), pid, **kwargs)
                    self._db().commit()
                    _json_response(self, 200, ti)
                except P.ProjectNameEmpty as e:
                    _json_error(self, 400, str(e))
                except P.LevelInvalid as e:
                    _json_error(self, 400, str(e))
                except P.NonPositiveNumber as e:
                    _json_error(self, 400, str(e))
                except P.TargetMustPositive as e:
                    _json_error(self, 400, str(e))
                except P.SDMustPositive as e:
                    _json_error(self, 400, str(e))
                except P.LotExists as e:
                    _json_error(self, 400, str(e))
                except LookupError as e:
                    _json_error(self, 404, str(e))
                return

            # measurements
            if path.startswith("/api/test_items/") and path.endswith("/measurements"):
                tid = int(path.split("/")[3])
                # 取 test_item 的 project_id
                ti = P.get_test_item(self._db(), tid)
                if ti is None:
                    _json_error(self, 404, "test_item not found")
                    return
                kwargs = {
                    "test_item_id": tid,
                    "project_id": ti.project_id,
                    "lot": data.get("lot") or ti.lot,
                    "level": data.get("level") or ti.level,
                    "date": data.get("date"),
                    "seq": int(data.get("seq", 0)),
                    "value": data.get("value"),
                    "note": data.get("note"),
                }
                # spec §0 名词表 + §B.1 录入表单：操作人员 / 前处理人员
                # 可空字段，前端未传时给 None（后端 create_measurement 默认 NULL 入库）
                if data.get("operator") is not None:
                    kwargs["operator"] = data["operator"]
                if data.get("pre_processor") is not None:
                    kwargs["pre_processor"] = data["pre_processor"]
                try:
                    m = M.create_measurement(self._db(), **kwargs)
                    self._db().commit()
                except M.ValueMustNonNegative as e:
                    _json_error(self, 400, str(e))
                    return
                except M.ValueInvalidNumber as e:
                    _json_error(self, 400, str(e))
                    return
                _json_response(self, 200, m)
                return

            # §A.4 添加新批号
            if path.startswith("/api/test_items/") and path.endswith("/lots"):
                tid = int(path.split("/")[3])
                try:
                    new_ti = P.add_lot(
                        self._db(), tid,
                        new_lot=data.get("new_lot") or "",
                        target=data.get("target"),
                        sd=data.get("sd"),
                        expiry_date=data.get("expiry_date"),
                        operator=data.get("operator"),
                        pre_processor=data.get("pre_processor"),
                    )
                    self._db().commit()
                    _json_response(self, 200, new_ti)
                except P.ProjectNameEmpty as e:
                    _json_error(self, 400, str(e))
                except P.NonPositiveNumber as e:
                    _json_error(self, 400, str(e))
                except P.TargetMustPositive as e:
                    _json_error(self, 400, str(e))
                except P.SDMustPositive as e:
                    _json_error(self, 400, str(e))
                except P.LotExists as e:
                    _json_error(self, 400, str(e))
                except LookupError as e:
                    _json_error(self, 404, str(e))
                return

            _json_error(self, 404, f"no route POST: {path}")

        def _post_save_investigation(self, mid: int) -> None:
            """§F.1 失控排查写库。

            POST /api/ooc/<mid>/investigate
            Body: {items: [{category, item}, ...], conclusion?: str}
            """
            from qc_tool import investigation as IN

            data = _read_json(self)
            items = data.get("items") or []
            conclusion = data.get("conclusion")
            if not items:
                _json_error(self, 400, "checklist 不能为空（spec §F.1 d）")
                return
            # 从 measurement 拿触发规则
            row = self._db().execute(
                "SELECT m.id, m.rules "
                "FROM measurements m WHERE m.id = ?", (mid,),
            ).fetchone()
            if row is None:
                _json_error(self, 404, f"measurement {mid} 不存在")
                return
            rules_str = row["rules"] or ""
            rules_list = [r.strip() for r in rules_str.split(",") if r.strip()]
            # checklist dict（spec §F.1 d 约定格式：{category: [item, ...]}）
            # save_investigation 实际期望 {item: bool}，但 investigation.py
            # 把整个 dict JSON 化保存，所以这里把 {cat: [items]} 当 checklist
            # 存入（前端会原样回读做展示）
            checklist: dict[str, list[str]] = {}
            for it in items:
                cat = it.get("category", "其他")
                item = it.get("item", "")
                if not item:
                    continue
                checklist.setdefault(cat, []).append(item)
            try:
                IN.save_investigation(
                    self._db(),
                    measurement_id=mid,
                    rules=rules_list,
                    checklist=checklist,
                    conclusion=conclusion,
                )
                self._db().commit()
                _json_response(self, 200, {"ok": True, "n_items": len(items)})
            except LookupError as e:
                _json_error(self, 404, str(e))
            except ValueError as e:
                _json_error(self, 400, str(e))
            except Exception as e:
                traceback.print_exc()
                _json_error(self, 500, f"{type(e).__name__}: {e}")

        def _post_import_xlsx(self) -> None:
            """§B.2 批量导入（spec V1.3 §B.2）。

            POST /api/test_items/<tid>/import
            Headers: X-Project-Name-Id = <project_id>
            Body: xlsx 文件原始字节
            """
            url = urlparse(self.path)
            path = url.path
            tid = int(path.split("/")[3])
            project_id_str = self.headers.get("X-Project-Name-Id", "").strip()
            if not project_id_str:
                _json_error(self, 400, "需要 X-Project-Name-Id header（项目 ID）")
                return
            try:
                project_id = int(project_id_str)
            except ValueError:
                _json_error(self, 400, "X-Project-Name-Id 必须是整数")
                return
            proj = P.get_project(self._db(), project_id)
            if proj is None:
                _json_error(self, 400, f"项目 ID {project_id} 不存在")
                return
            project_name = proj.name

            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                plan = X.parse_import_xlsx_bytes(raw)
            except M.MissingRequiredColumns as e:
                _json_error(self, 400, f"缺少必需列：{', '.join(e.columns)}")
                return

            for r in plan.ok_rows:
                r.project_name = project_name

            try:
                def _project_resolver(name: str):
                    if name == project_name:
                        return project_id
                    return None

                def _ti_resolver(name: str, pname: str, level: str):
                    row = self._db().execute(
                        "SELECT t.id FROM test_items t "
                        "JOIN projects p ON p.id = t.project_id "
                        "WHERE p.name = ? AND t.name = ? AND t.level = ?",
                        (pname, name, level),
                    ).fetchone()
                    return row["id"] if row else None

                result = X.import_rows(
                    self._db(), plan,
                    project_resolver=_project_resolver,
                    test_item_resolver=_ti_resolver,
                    skip_duplicates=True,
                )
                self._db().commit()
                # 把 plan.skipped_rows 也带上（spec §B.2 错误报告要写明行号）
                result["skipped_rows"] = [
                    {"original_row_no": s.original_row_no, "reason": s.reason}
                    for s in plan.skipped_rows
                ]
                _json_response(self, 200, result)
            except X.ProjectNotFound as e:
                self._db().rollback()
                _json_error(self, 400, str(e))
            except X.TestItemNotFound as e:
                self._db().rollback()
                _json_error(self, 400, str(e))
            except M.DuplicateImportRows as e:
                self._db().rollback()
                _json_error(self, 400, str(e))

        def _post_preview_import_xlsx(self) -> None:
            """数据导入预解析与预览（不写库）。

            POST /api/test_items/<tid>/preview_import
            Headers: X-Project-Name-Id = <project_id>
            Body: xlsx 文件原始字节
            """
            url = urlparse(self.path)
            path = url.path
            tid = int(path.split("/")[3])
            project_id_str = self.headers.get("X-Project-Name-Id", "").strip()
            if not project_id_str:
                _json_error(self, 400, "需要 X-Project-Name-Id header（项目 ID）")
                return
            try:
                project_id = int(project_id_str)
            except ValueError:
                _json_error(self, 400, "X-Project-Name-Id 必须是整数")
                return
            proj = P.get_project(self._db(), project_id)
            if proj is None:
                _json_error(self, 400, f"项目 ID {project_id} 不存在")
                return
            project_name = proj.name

            content_len = int(self.headers.get("Content-Length", 0))
            if content_len <= 0:
                _json_error(self, 400, "上传文件为空")
                return
            raw = self.rfile.read(content_len)

            try:
                plan = X.parse_import_xlsx_bytes(raw)
            except M.MissingRequiredColumns as e:
                _json_error(self, 400, f"缺少必需列：{', '.join(e.columns)}")
                return
            except Exception as e:
                _json_error(self, 400, f"Excel 文件无法解析：{e}")
                return

            # 查询当前项目下所有检测指标，以便在预览阶段检查指标是否存在
            existing_items = self._db().execute(
                "SELECT id, name, level, lot FROM test_items WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            existing_map = {(row["name"], row["level"]): row["id"] for row in existing_items}

            ok_rows_data = []
            skipped_rows_data = [
                {"original_row_no": s.original_row_no, "reason": s.reason}
                for s in plan.skipped_rows
            ]

            for r in plan.ok_rows:
                # 检查项目名称一致性
                if r.project_name and r.project_name != project_name:
                    skipped_rows_data.append({
                        "original_row_no": r.original_row_no,
                        "reason": f"第 {r.original_row_no} 行项目名「{r.project_name}」与当前选定项目「{project_name}」不符",
                    })
                    continue

                # 检查指标名及水平是否存在
                ti_id = existing_map.get((r.test_item_name, r.level))
                if not ti_id:
                    skipped_rows_data.append({
                        "original_row_no": r.original_row_no,
                        "reason": f"第 {r.original_row_no} 行指标「{r.test_item_name} ({r.level})」在当前项目中不存在",
                    })
                    continue

                # 检查是否为库中已有数据（增量识别）
                existing_m = self._db().execute(
                    "SELECT 1 FROM measurements WHERE test_item_id = ? AND lot = ? AND level = ? AND date = ? AND seq = ? LIMIT 1",
                    (ti_id, r.lot, r.level, r.date, r.seq),
                ).fetchone()
                is_duplicate = existing_m is not None

                ok_rows_data.append({
                    "original_row_no": r.original_row_no,
                    "project_name": project_name,
                    "test_item_name": r.test_item_name,
                    "level": r.level,
                    "lot": r.lot,
                    "date": r.date,
                    "value": r.value,
                    "seq": r.seq,
                    "is_duplicate": is_duplicate,
                })

            # 按原始行号排序
            ok_rows_data.sort(key=lambda x: x["original_row_no"])
            skipped_rows_data.sort(key=lambda x: x["original_row_no"])

            new_insert_count = sum(1 for r in ok_rows_data if not r["is_duplicate"])
            duplicate_count = sum(1 for r in ok_rows_data if r["is_duplicate"])

            _json_response(self, 200, {
                "can_import": new_insert_count > 0,
                "project_name": project_name,
                "total_count": len(ok_rows_data) + len(skipped_rows_data),
                "ok_count": len(ok_rows_data),
                "new_count": new_insert_count,
                "duplicate_count": duplicate_count,
                "skipped_count": len(skipped_rows_data),
                "ok_rows": ok_rows_data,
                "skipped_rows": skipped_rows_data,
            })

        def _post_calc_target_sd(self) -> None:
            """解析上传的 Excel 原始测定值字节，测算各指标及 L/H 靶值与 SD。"""
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                _json_error(self, 400, "上传文件为空")
                return

            raw = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")
            excel_bytes = raw

            if "multipart/form-data" in content_type:
                # 方式 1：利用 email MIME 解析器提取二进制附件
                extracted = None
                try:
                    from email import message_from_bytes
                    mime_header = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
                    msg = message_from_bytes(mime_header + raw)
                    for part in msg.walk():
                        if part.get_filename() or part.get_content_maintype() not in ("multipart", "text"):
                            payload = part.get_payload(decode=True)
                            if payload and len(payload) > 100:
                                extracted = payload
                                break
                except Exception:
                    pass

                # 方式 2：基于 PK 压缩包头（.xlsx 为 zip）定位与 boundary 截断
                if not extracted:
                    pk_pos = raw.find(b"PK\x03\x04")
                    if pk_pos != -1:
                        boundary = ""
                        for seg in content_type.split(";"):
                            seg = seg.strip()
                            if seg.startswith("boundary="):
                                boundary = seg.split("=", 1)[1].strip("\"' ")
                                break
                        file_part = raw[pk_pos:]
                        if boundary:
                            b_boundary = f"--{boundary}".encode("utf-8")
                            b_idx = file_part.find(b_boundary)
                            if b_idx != -1:
                                file_part = file_part[:b_idx]
                                if file_part.endswith(b"\r\n"):
                                    file_part = file_part[:-2]
                        extracted = file_part

                if extracted:
                    excel_bytes = extracted
                elif b"\r\n\r\n" in raw:
                    parts = raw.split(b"\r\n\r\n", 1)
                    if len(parts) > 1:
                        excel_bytes = parts[1]

            try:
                result = X.calculate_target_sd_from_xlsx_bytes(excel_bytes)
                _json_response(self, 200, result)
            except Exception as e:
                _json_error(self, 400, f"测算失败: {str(e)}")

        def _restore_backup(self, bname: str) -> None:
            """从备份安全恢复（spec §8.2）。"""
            from .backup import BACKUP_DIRNAME
            import shutil
            # 防路径穿越安全检查
            if "/" in bname or "\\" in bname or ".." in bname:
                _json_error(self, 400, "非法文件名")
                return
            bdir = db_path.parent / BACKUP_DIRNAME
            src = bdir / bname
            if not src.is_file() or src.suffix != ".db":
                _json_error(self, 404, "备份文件不存在")
                return
            # 恢复前先对当前 db 做一份救急备份
            try:
                if db_path.exists():
                    emergency_backup = bdir / f"emergency_before_restore_{date.today().strftime('%Y%m%d%H%M%S')}.db"
                    shutil.copy2(db_path, emergency_backup)
                # 覆盖当前 db
                shutil.copy2(src, db_path)
                # 重新初始化连接
                init_database(db_path)
                _json_response(self, 200, {"ok": True, "restored_from": bname})
            except Exception as e:
                traceback.print_exc()
                _json_error(self, 500, f"恢复失败: {e}")

        # ---- PUT / DELETE（Step 0 路由框架 → Step 1 实现具体 handler）----

        def do_PUT(self) -> None:  # noqa: N802
            try:
                self._dispatch_put()
            except Exception as e:
                traceback.print_exc()
                _json_error(self, 500, f"{type(e).__name__}: {e}")

        def do_DELETE(self) -> None:  # noqa: N802
            try:
                self._dispatch_delete()
            except Exception as e:
                traceback.print_exc()
                _json_error(self, 500, f"{type(e).__name__}: {e}")

        def _dispatch_put(self) -> None:
            """PUT 路由分发（spec §A.3.1 / §A.3.2 / §B.4）。"""
            url = urlparse(self.path)
            path = url.path
            data = _read_json(self)

            # 项目编辑
            if path.startswith("/api/projects/") and path.count("/") == 3:
                pid = int(path.split("/")[3])
                try:
                    kwargs = {k: data.get(k) for k in
                              ("name", "short", "unit", "manufacturer")}
                    p = P.update_project(self._db(), pid, **kwargs)
                    self._db().commit()
                    _json_response(self, 200, p)
                except P.ProjectNameEmpty as e:
                    _json_error(self, 400, str(e))
                except P.ProjectNameExists as e:
                    _json_error(self, 400, str(e))
                except LookupError as e:
                    _json_error(self, 404, str(e))
                return

            # 检测指标编辑
            if path.startswith("/api/test_items/") and path.count("/") == 3:
                tid = int(path.split("/")[3])
                try:
                    kwargs = {k: data.get(k) for k in
                              ("name", "short", "unit", "manufacturer", "lot", "level",
                               "target", "sd", "expiry_date",
                               "source", "operator", "pre_processor")}
                    # 去掉 None 值，避免覆盖原值
                    kwargs = {k: v for k, v in kwargs.items() if v is not None}
                    ti = P.update_test_item(self._db(), tid, **kwargs)
                    self._db().commit()
                    _json_response(self, 200, ti)
                except P.NonPositiveNumber as e:
                    _json_error(self, 400, str(e))
                except P.TargetMustPositive as e:
                    _json_error(self, 400, str(e))
                except P.SDMustPositive as e:
                    _json_error(self, 400, str(e))
                except P.LevelInvalid as e:
                    _json_error(self, 400, str(e))
                except P.ProjectNameEmpty as e:
                    _json_error(self, 400, str(e))
                except LookupError as e:
                    _json_error(self, 404, str(e))
                return

            # measurement 编辑（spec §B.4 a：5 字段任选）
            if path.startswith("/api/measurements/") and path.count("/") == 3:
                mid = int(path.split("/")[3])
                try:
                    m = M.update_measurement_fields(
                        self._db(), mid,
                        date=data.get("date"),
                        level=data.get("level"),
                        lot=data.get("lot"),
                        seq=data.get("seq"),
                        value=data.get("value"),
                    )
                    self._db().commit()
                    _json_response(self, 200, m)
                except M.ValueInvalidNumber as e:
                    _json_error(self, 400, str(e))
                except M.ValueMustNonNegative as e:
                    _json_error(self, 400, str(e))
                except LookupError as e:
                    _json_error(self, 404, str(e))
                except ValueError as e:
                    _json_error(self, 400, str(e))
                return

            _json_error(self, 404, f"no route PUT: {path}")

        def _dispatch_delete(self) -> None:
            """DELETE 路由分发（spec §A.3.1 / §A.3.2）。"""
            url = urlparse(self.path)
            path = url.path

            # 项目删除
            if path.startswith("/api/projects/") and path.count("/") == 3:
                pid = int(path.split("/")[3])
                try:
                    summary = P.delete_project(self._db(), pid)
                    self._db().commit()
                    _json_response(self, 200, {
                        "n_test_items": summary.n_test_items,
                        "n_measurements": summary.n_measurements,
                        "n_out_of_control": summary.n_out_of_control,
                    })
                except LookupError as e:
                    _json_error(self, 404, str(e))
                return

            # 检测指标删除
            if path.startswith("/api/test_items/") and path.count("/") == 3:
                tid = int(path.split("/")[3])
                try:
                    summary = P.delete_test_item(self._db(), tid)
                    self._db().commit()
                    _json_response(self, 200, {
                        "n_measurements": summary.n_measurements,
                        "n_out_of_control": summary.n_out_of_control,
                    })
                except P.HasUnexportedOOC as e:
                    _json_error(self, 400, str(e))
                except LookupError as e:
                    _json_error(self, 404, str(e))
                return

            # measurement 标记作废（spec §B.4 d，幂等）
            if path.startswith("/api/measurements/") and path.count("/") == 3:
                mid = int(path.split("/")[3])
                try:
                    m = M.mark_void(self._db(), mid)
                    self._db().commit()
                    _json_response(self, 200, m)
                except LookupError as e:
                    _json_error(self, 404, str(e))
                return

            _json_error(self, 404, f"no route DELETE: {path}")

        def _dispatch_method_not_implemented(self, method: str) -> None:
            """保留：未实现的具体方法返 501。"""
            url = urlparse(self.path)
            _json_error(
                self, 501,
                f"{method} {url.path} 暂未实现（Step 0 占位，将在后续 Step 实现）",
            )

    return Handler


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

def _find_free_port(host: str, start: int) -> int:
    import socket
    for p in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"no free port in [{start}, {start+50})")


def main(argv: list[str] | None = None) -> int:
    """启动 Web 服务器，浏览器自动打开。

    参数（可选）：
        --port 8765        # 指定端口（默认 8765）
        --no-browser       # 不自动打开浏览器
    """
    import argparse
    parser = argparse.ArgumentParser(prog="qc_tool.web")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv[1:] if argv else None)

    # 1) DB 路径：exe/cwd/qc.db
    db_path = Path.cwd() / "qc.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 2) 启动时备份
    backup.startup_check_and_backup(db_path)
    # 标记 dirty：web server 进程是"运行中"
    backup.mark_exit_dirty(db_path.parent)

    # 3) 找空闲端口
    host = "127.0.0.1"
    port = _find_free_port(host, args.port)
    if port != args.port:
        print(f"[web] 端口 {args.port} 被占用，改用 {port}")

    # 4) 起服务
    handler = make_handler(db_path=db_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.db_path = db_path

    url = f"http://{host}:{port}/"
    print(f"[web] QC Tool Web 预览已启动")
    print(f"[web]   地址：{url}")
    print(f"[web]   DB  ：{db_path}")
    print(f"[web]   关闭：Ctrl+C")
    print(f"[web]   业务模块：projects / measurements / lj_chart / reports / statistics / investigation / backup")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 收到 Ctrl+C，正在关闭...")
    finally:
        # 标记 clean：用户主动关闭
        backup.mark_exit_clean(db_path.parent)
        httpd.shutdown()
        httpd.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
