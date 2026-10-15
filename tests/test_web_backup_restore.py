"""
PR#7 — spec V1.3 §8.2 备份管理与从备份恢复测试

覆盖：
1. HTML 包含 openBackups、restoreBackup 函数与「恢复此备份」操作按钮
2. 后端 POST /api/backups/<name>/restore 端点实现：
   - 路径穿越安全防护（拦截 ../, /, \\ 等攻击）
   - 404 拦截不存在的备份
   - 正确覆盖当前 qc.db 并在覆盖前生成紧急回滚备份
"""
from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError

import pytest
from qc_tool.backup import BACKUP_DIRNAME


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from qc_tool import web as webmod
    port = _free_port()
    handler = webmod.make_handler(db_path=tmp_path / "qc.db")
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.db_path = tmp_path / "qc.db"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    time.sleep(0.05)
    try:
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _get_html(base: str) -> str:
    return urlreq.urlopen(f"{base}/", timeout=5).read().decode("utf-8")


def _http(method: str, url: str, data: dict | None = None) -> tuple[int, bytes]:
    body = json.dumps(data).encode("utf-8") if data is not None else b""
    req = urlreq.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"}
    )
    resp = urlreq.urlopen(req, timeout=5)
    return resp.status, resp.read()


def test_html_has_backup_restore_ui(web_server):
    """静态扫描 HTML 存在 openBackups、restoreBackup 与恢复按钮。"""
    base, _ = web_server
    html = _get_html(base)
    assert "openBackups" in html
    assert "restoreBackup" in html
    assert "恢复此备份" in html
    assert "/restore" in html


def test_restore_backup_rejects_path_traversal(web_server):
    """验证 POST restore 严格拦截路径穿越。"""
    base, _ = web_server
    # 尝试传 ../other.db
    with pytest.raises(HTTPError) as excinfo:
        _http("POST", f"{base}/api/backups/..%2Fhack.db/restore")
    assert excinfo.value.code == 400


def test_restore_backup_not_found(web_server):
    """验证恢复不存在的文件返回 404。"""
    base, _ = web_server
    with pytest.raises(HTTPError) as excinfo:
        _http("POST", f"{base}/api/backups/nonexistent_file.db/restore")
    assert excinfo.value.code == 404


def test_restore_backup_success(web_server):
    """端到端验证真实备份恢复流程。"""
    base, tmp_path = web_server
    # 1. 初始状态：建项目 A
    _, res = _http("POST", f"{base}/api/projects", {"name": "初始项目A"})
    pid_a = json.loads(res)["id"]

    # 2. 模拟系统生成了一份备份 qc_20260401.db 到 backup 目录
    bdir = tmp_path / BACKUP_DIRNAME
    bdir.mkdir(parents=True, exist_ok=True)
    backup_file = bdir / "qc_20260401.db"
    shutil.copy2(tmp_path / "qc.db", backup_file)

    # 3. 在当前库新建项目 B
    _, res = _http("POST", f"{base}/api/projects", {"name": "新项目B"})

    # 验证当前库有两个项目
    _, res = _http("GET", f"{base}/api/projects")
    projects_before = json.loads(res)
    assert len(projects_before) == 2

    # 4. 执行从备份恢复
    status, res = _http("POST", f"{base}/api/backups/qc_20260401.db/restore")
    assert status == 200
    res_data = json.loads(res)
    assert res_data["ok"] is True

    # 5. 验证恢复后库只剩项目 A（项目 B 被成功回退），并生成了 emergency 备份
    _, res = _http("GET", f"{base}/api/projects")
    projects_after = json.loads(res)
    assert len(projects_after) == 1
    assert projects_after[0]["name"] == "初始项目A"

    emergency_files = list(bdir.glob("emergency_before_restore_*.db"))
    assert len(emergency_files) >= 1, "恢复前应产生紧急安全备份"
