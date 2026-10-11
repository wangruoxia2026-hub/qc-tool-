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
import sqlite3
import sys
import traceback
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from .db import get_connection, init_database
from . import projects as P
from . import measurements as M
from . import lj_chart as LJ
from . import reports as R
from . import statistics as S
from . import investigation as I
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
                self._render_chart_svg(tid, year, month, status_filter)
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

        def _render_chart_svg(self, tid: int, year: int, month: int, status_filter: str | None) -> None:
            ti = P.get_test_item(self._db(), tid)
            if ti is None:
                _json_error(self, 404, "test_item not found")
                return
            proj = P.get_project(self._db(), ti.project_id)
            if proj is None:
                _json_error(self, 404, "project not found")
                return
            data = LJ.compute_chart_data(
                self._db(),
                project_id=ti.project_id,
                test_item_name=ti.name,
                lot=ti.lot,
                level=ti.level,
                year=year, month=month,
            )
            # 状态过滤
            if status_filter:
                data = self._filter_chart_data(data, status_filter)
            svg = self._data_to_svg(data, ti)
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

        def _data_to_svg(self, data, ti) -> bytes:
            """把 LJChartData 渲染成 SVG（不依赖 matplotlib，浏览器内交互）。"""
            W, H = 900, 480
            ml, mr, mt, mb = 70, 30, 50, 60
            inner_w = W - ml - mr
            inner_h = H - mt - mb

            target = ti.target
            sd = ti.sd
            if sd <= 0:
                return f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"><text x="20" y="40" fill="red">SD=0，控制限无效</text></svg>'.encode("utf-8")

            # Y 范围 = ±4SD
            y_min, y_max = target - 4 * sd, target + 4 * sd
            y_range = y_max - y_min or 1

            def y_to_px(v):
                return mt + inner_h * (1 - (v - y_min) / y_range)

            # X 轴：日期
            pts = data.points
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

            # 标题
            out.append(f'<text x="{W/2}" y="20" text-anchor="middle" font-size="14" font-weight="bold">'
                       f'{ti.name} · {ti.lot} · {ti.level} · {data.year}-{data.month:02d} LJ 图</text>')

            # ±4SD 范围内白底绘图区
            out.append(f'<rect x="{ml}" y="{mt}" width="{inner_w}" height="{inner_h}" fill="white" stroke="#ccc"/>')

            # 控制限线（±3, ±2, ±1, 0）
            for n, color, dash in [(-3, "#c0392b", "4,2"), (-2, "#e67e22", "4,2"),
                                    (2, "#e67e22", "4,2"), (3, "#c0392b", "4,2"),
                                    (-1, "#3498db", "2,2"), (1, "#3498db", "2,2"),
                                    (0, "#2c3e50", "")]:
                v = target + n * sd
                if y_min <= v <= y_max:
                    y = y_to_px(v)
                    out.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+inner_w}" y2="{y:.1f}" '
                               f'stroke="{color}" stroke-width="1" stroke-dasharray="{dash}"/>')
                    out.append(f'<text x="{ml+inner_w+4}" y="{y+4:.1f}" fill="{color}" font-size="10">'
                               f'{"+" if n>0 else ""}{n}SD</text>')

            # 靶值文字
            out.append(f'<text x="{ml-6}" y="{y_to_px(target)+4:.1f}" text-anchor="end" fill="#2c3e50" font-size="10">'
                       f'{target:.2f}</text>')

            # 点
            color_map = {
                "in_control": "#27ae60",
                "warning": "#f39c12",
                "out_of_control": "#c0392b",
                "void": "#999",
            }
            for i, p in enumerate(pts):
                v = target + p.sd_score * sd
                if not (y_min <= v <= y_max):
                    continue
                x = x_to_px(i)
                y = y_to_px(v)
                color = color_map.get(p.status.value if hasattr(p.status, "value") else str(p.status), "#27ae60")
                # 失控用大三角
                if (hasattr(p.status, "value") and p.status.value == "out_of_control") or str(p.status) == "out_of_control":
                    out.append(f'<polygon points="{x:.1f},{y-7:.1f} {x-7:.1f},{y+5:.1f} {x+7:.1f},{y+5:.1f}" '
                               f'fill="{color}" stroke="#000"><title>{p.date} seq={p.seq} value={p.value:.3f} '
                               f'sd={p.sd_score:+.2f} rules={",".join(p.rules) or "—"}</title></polygon>')
                else:
                    out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" stroke="#000" stroke-width="0.5">'
                               f'<title>{p.date} seq={p.seq} value={p.value:.3f} sd={p.sd_score:+.2f}</title></circle>')
                # 折线
                if i > 0:
                    prev_v = target + pts[i-1].sd_score * sd
                    if y_min <= prev_v <= y_max and y_min <= v <= y_max:
                        px = x_to_px(i-1)
                        py = y_to_px(prev_v)
                        out.append(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{x:.1f}" y2="{y:.1f}" stroke="#666" stroke-width="0.8"/>')

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
                ("在控", "#27ae60", "circle"),
                ("警告", "#f39c12", "circle"),
                ("失控", "#c0392b", "triangle"),
                ("作废", "#999", "circle"),
            ]:
                if shape == "triangle":
                    out.append(f'<polygon points="{lx+5},{ly-6} {lx-2},{ly+4} {lx+12},{ly+4}" fill="{color}"/>')
                else:
                    out.append(f'<circle cx="{lx+5}" cy="{ly}" r="4" fill="{color}"/>')
                out.append(f'<text x="{lx+18}" y="{ly+4}" font-size="10">{label}</text>')
                lx += 70

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
            # 选所有 test_items（不弹"选择指标"对话框，全选）
            items = P.list_test_items(self._db(), pid)
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
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
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

            # §F.1 失控排查
            if path.startswith("/api/ooc/") and path.endswith("/investigate"):
                mid = int(path.split("/")[3])
                self._post_save_investigation(mid)
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
                    for k in ("short", "unit", "manufacturer", "expiry_date"):
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
                              ("short", "unit", "manufacturer", "lot", "level",
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
