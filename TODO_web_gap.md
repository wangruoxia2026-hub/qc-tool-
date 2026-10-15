# Spec V1.3 vs Web 现状 缺口清单（2026-10-11 审计）

> 状态：**仅审计，未动手**。实施计划见仓库根 `state.json` 的 `plan` 字段。
> 审计依据：spec V1.3 全文 671 行 + `qc_tool/web.py` 1012 行 + `index.html` 812 行
> + 6 个 `test_web_*.py` + 业务层 `projects.py` / `measurements.py` / `excel_io.py` / `lj_chart.py` / `reports.py`。

---

## 总览

| 状态 | 验收点数 | 说明 |
| --- | --- | --- |
| ✅ 已就绪 | 11 | A.1 / A.2 / A.3.1 / A.3.2 / A.4 / A.5 / B.1 / D / E.1(图) / F.1 |
| ❌ 必补 | 8 | B.2 / B.3 / B.4 / C.3 / E.1(配置) / G.1 / G.3 / 8.2 |
| ⚠ 半成品 | 2 | F.1 e 多规则合并 / E.1 tooltip 增强 |
| 🚫 §10 范围外 | 15 项 | 严守不动 |

---

## G1 — §B.2 Excel 批量导入

### spec 节录
> 用户操作：点击「导入」→ 选择本工具提供的 .xlsx 模板（**V1.3 模板新增"检测指标"列**）→ 选择文件 → **全量预览** → 确认导入。

### 现状
- ✅ 后端 `POST /api/test_items/<id>/import` 已实现，5 个测试绿（`test_web_a4_b2_b3.py::TestB2Import`）。
- ❌ 前端 HTML 无「导入」按钮、无 `<input type=file">`、无 `FormData` 提交。

### 修复
**`qc_tool/web/templates/index.html`**：在「测量列表」标题栏加：
```html
<button class="secondary" onclick="document.getElementById('file-import').click()">导入 Excel</button>
<input type="file" id="file-import" accept=".xlsx" style="display:none">
```
**JS**（20 行）：监听 file change → 拼 FormData → POST 到 `/api/test_items/<tid>/import` →
弹 toast 显示 success/failed 计数 + skipped_rows 错误列表。

**新测试** `tests/test_web_b2_buttons.py`：
1. `test_html_has_import_button_and_file_input` — 静态扫描 HTML 含 `id="btn-import"` / `id="file-import"`
2. `test_import_button_triggers_file_input` — 静态扫描 JS 含 `file-import.click()`
3. `test_html_post_import_with_filedata` — 起 web server 走端到端：建项目 + 指标 + 上传 50 行 xlsx + 验证 imported_count==50
4. `test_html_import_shows_skipped_rows` — 端到端：上传 3 行 1 行日期错 → 验证 toast 文本含 "日期" 字样

### 范围外（绝不做）
- 不做「全量预览」弹窗（spec 写"全量预览 → 确认"，本期简化为直接导入，验收仅看 50 行入库）。
- 不做「已存在批号覆盖」二次确认（spec §B.2 g 走 §A.4 路径，后端已报 400 弹窗即可）。

---

## G2 — §B.3 下载 Excel 模板

### 修复
**HTML**：加 `<button class="secondary" id="btn-template" onclick="downloadTemplate()">下载模板</button>`。

**JS**（10 行）：
```js
async function downloadTemplate() {
  const r = await fetch('/api/template.xlsx');
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'qc_template.xlsx';
  a.click();
  URL.revokeObjectURL(a.href);
}
```

**新测试** `tests/test_web_b3_button.py`：
1. `test_html_has_template_button_with_handler` — 静态扫描含 `downloadTemplate` / `template.xlsx`

---

## G3 — §B.4 编辑值 / 标记作废

### 现状
- ✅ 业务层 `M.mark_void(conn, mid)` 已实现（L329-344）—— 改 status='作废'，不物理删
- ✅ `M.update_measurement_value(conn, mid, value)` 已实现
- ✅ 后端 `PUT /api/measurements/<id>` 已通（测过改值）
- ❌ 后端无 `DELETE /api/measurements/<id>` 端点
- ❌ 前端无右键菜单 / 行内操作按钮

### 修复

**`qc_tool/web.py`**（35 行）：
- 加 `do_DELETE` 分发 `DELETE /api/measurements/<id>` → 调 `M.mark_void` → 200
- 注：spec §10 第 12 条禁止「单条物理删除」；`mark_void` 是软作废（status='作废'），符合 spec
- 路径分发时 path 形如 `/api/measurements/<id>`，与 `/api/measurements/.../import` 不同（中间段数 = 3）

**HTML**（10 行 + 60 行 JS）：
- 测量表行加 `oncontextmenu="showRowMenu(event, m.id)"` + 全局监听 click 关闭菜单
- 右键菜单：编辑 / 标记作废 / 取消
- 编辑弹 modal：输入新测定值 → PUT → 刷新
- 标记作废：confirm() → DELETE → 刷新

**新测试** `tests/test_web_b4_void.py`：
1. `test_DELETE_measurement_marks_void_status` — 起服务 → 录入 → DELETE → DB status='作废'
2. `test_DELETE_already_void_idempotent` — 重复 DELETE 不报错
3. `test_html_has_row_contextmenu_handler` — 静态扫描含 `oncontextmenu`
4. `test_html_void_action_calls_DELETE` — 静态扫描含 `/api/measurements/` + `DELETE`

### 范围外
- 不做「撤销作废」（spec §B.4 文本只写"标记作废"单向）
- 不做「批量作废」

---

## G4 — §C.3 source 切换

### 现状
- ✅ 业务层 `update_test_item` 已支持 `source` 字段（L640-642）
- ✅ 后端 `PUT /api/test_items/<id>` 已通
- ❌ 前端编辑指标 modal 缺 `source` 选择

### 修复
**HTML**（4 行）：在 editTestItem modal 加：
```html
<div class="form-row"><label>统计模式</label>
  <select id="m-e-ti-source">
    <option value="固定">固定（用靶值/SD）</option>
    <option value="累计">累计（按历史重算）</option>
  </select>
</div>
```
**JS**（6 行）：submit 时塞 `source: $("m-e-ti-source").value`。

**新测试** `tests/test_web_c3_source.py`：
1. `test_html_edit_ti_has_source_select` — 静态扫描含 `id="m-e-ti-source"` + 两 option

### 范围外
- 不做"实时预览"切换效果（PUT 后刷新列表即生效）

---

## G5 — §E.1 d 图表配置 12 元信息勾选

### 现状
- ✅ SVG 渲染固定全开 12 元信息
- ❌ 无勾选面板

### 修复
**`qc_tool/web.py`**（40 行）：`_render_chart_svg` 接受新 query `info=<csv>` → 按 list 过滤元信息显示块。

**HTML**（60 行 + 80 行 JS）：
- 加「图表配置」按钮 → 弹 modal
- 12 个 checkbox：项目名称 / 检测指标 / 简称 / 单位 / 厂家 / 批号 / 水平 / 靶值 / SD / 操作人员 / 前处理人员 / 时间区间
- 选中状态持久化到 `localStorage['qc_chart_config']`
- `refreshChart()` 时把选中 list 作为 `&info=` query

**新测试** `tests/test_web_e1_config.py`：
1. `test_GET_chart_svg_with_info_filter` — 后端：上传 12 元 info 全选 vs 只选 1 个，验证 SVG 内容差异
2. `test_html_has_chart_config_button_and_12_checkboxes` — 静态扫描

### 范围外
- 不做"配置导入导出"（仅 localStorage 单机）
- 不做"全部选中/全部取消"快捷键

---

## G6 — §G.1 选择导出指标

### 现状
- ❌ 后端 L447 直接 `selected_test_item_ids=[it.id for it in items]` 全选
- ❌ 前端无对话框

### 修复
**`qc_tool/web.py`**（15 行）：`_export_monthly` 接 `?selected=<csv>` query；空时走全选（兼容）。

**HTML**（30 行 + 50 行 JS）：
- openReports 把「下载月度质控报告 xlsx」按钮改为「选择导出指标」按钮
- 弹 modal：列出当前项目所有 test_item，每个一行 checkbox `id="exp-<tid>"`，显示 `指标名·批号·水平`
- 底部「下载」按钮：收集选中 ID → 拼到 URL → 触发下载

**新测试** `tests/test_web_g1_export.py`：
1. `test_GET_monthly_xlsx_with_selected_ids` — 上传 2 个指标 (D2-L / D2-H)，只勾 D2-L → 验证 xlsx 只 1 个 sheet 组
2. `test_html_has_export_indicator_modal` — 静态扫描含 `id="modal-export"` + 多 checkbox 模板

### 范围外
- 不做"按月过滤指标"（spec 只要求按 (指标, 水平) 选）

---

## G7 — §G.3 导出质控图 PNG

### 现状
- ✅ SVG 渲染 OK
- ❌ 无「导出图片」按钮

### 修复
**HTML**（3 行）：chart-toolbar 加 `<button class="secondary" id="btn-export-png">导出图片</button>`。
**JS**（30 行）：
```js
async function exportPng() {
  const svgUrl = $("chart-img").src;
  const r = await fetch(svgUrl);
  const svgText = await r.text();
  const blob = new Blob([svgText], {type: 'image/svg+xml'});
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement('canvas');
    canvas.width = 1200; canvas.height = 640;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 1200, 640);
    ctx.drawImage(img, 0, 0, 1200, 640);
    canvas.toBlob((b) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(b);
      a.download = `qc_${state.currentTestItem.name}_${$("chart-year").value}-${$("chart-month").value}.png`;
      a.click();
    });
  };
  img.src = url;
}
```

**新测试** `tests/test_web_g3_png.py`：
1. `test_html_has_export_png_button` — 静态扫描

### 范围外
- 不做"复制到剪贴板"
- 不做"打印预览"（spec §10 第 7 条）

---

## G8 — §8.2 从备份恢复

### 现状
- ✅ 业务层 `backup.startup_check_and_backup` / `mark_exit_*` 已通
- ❌ 后端无 `POST /api/backups/<name>/restore`
- ❌ 前端 backup modal 无「恢复」按钮（L807 注释占位）

### 修复
**`qc_tool/web.py`**（50 行）：
- 加 `do_POST` 分发 `POST /api/backups/<name>/restore`
- 流程：
  1. 验证 name 形如 `qc_YYYYMMDD.db` 且只含 `[a-zA-Z0-9_.]`（防路径穿越）
  2. `shutil.copy2(backup_dir/name, db_path)` —— **不**先备份当前（备份逻辑交给 `backup.startup_check_and_backup` 下次启动时做）
  3. 200 + 提示"已恢复，下次启动生效；当前内存中数据保留到关闭"
- **不重启 server**：spec §8.2 仅要求恢复文件即可，连接已开，继续操作的是恢复后的数据

**HTML**（25 行 + 35 行 JS）：
- openBackups modal 每行加「恢复」按钮
- 点击 → confirm() → POST → 弹"恢复成功，请关闭浏览器重启"toast

**新测试** `tests/test_web_backup_restore.py`：
1. `test_GET_backups_returns_list_with_size` — 已覆盖在初始 L515 端点
2. `test_POST_restore_copies_backup_to_qc_db` — 端到端：建项目 + 录 1 条 → 手动 `backup.startup_backup` → 录第 2 条 → 删项目 → POST /api/backups/<name>/restore → 验证 qc.db 回到第 1 条状态
3. `test_POST_restore_path_traversal_400` — name 传 `../etc/passwd` 返 400
4. `test_html_has_restore_button_in_modal` — 静态扫描

### 范围外
- 不做"自动定期备份"（spec §8.2 只写"每天 1 份"，靠 `startup_check_and_backup` 实现，不做 UI）

---

## 实施计划（state.json.plan.prs_ordered）

| PR | 标题 | 含 G | 预计行数 | 新测试 |
| --- | --- | --- | --- | --- |
| 1 | feat(web): §B.2 批量导入 + §B.3 下载模板 (前端入口) | G1+G2 | HTML 35 + JS 37 | 4 |
| 2 | feat(web): §B.4 编辑/标记作废（后端 API + 前端右键菜单） | G3 | HTML 15 + JS 60 + PY 35 | 4 |
| 3 | feat(web): §C.3 source 固定/累计切换 UI | G4 | HTML 4 + JS 6 | 1 |
| 4 | feat(web): §E.1 图表配置 12 元信息勾选 | G5 | HTML 60 + JS 80 + PY 40 | 2 |
| 5 | feat(web): §G.1 选择导出指标对话框 | G6 | HTML 30 + JS 50 + PY 15 | 2 |
| 6 | feat(web): §G.3 导出质控图 PNG | G7 | HTML 3 + JS 30 | 1 |
| 7 | feat(web): §8.2 从备份恢复 | G8 | HTML 25 + JS 35 + PY 50 | 3 |
| 8 | build: PyInstaller 单文件 exe 化 + 桌面入口 | — | PY 80 | 2 |

**合计**：HTML 172 + JS 298 + PY 220 + 新测试 19 个

**保留约束**：
- AGENTS.md 铁律 1：判定逻辑（westgard.py + measurements.py）零修改
- AGENTS.md 铁律 2：§10 范围外 15 条一项都不做
- AGENTS.md 铁律 5：每个 PR 必带测试
- AGENTS.md 铁律 6：spec 歧义先报告

**双入口共存**：
- `python -m qc_tool` → web（默认）
- `python -m qc_tool --exe` → PyQt6 老 UI（沿用 ui_main.py + main_window_b.py）

---

## 已知待你确认的歧义（不私自决定，AGENTS.md 铁律 6） — 已全部拍板

1. **G1 §B.2 全量预览** → **✅ 实现预览弹窗**（用户拍板"full-preview"）
2. **G8 §8.2 恢复后是否要自动重载** → **✅ 恢复后 server 主动退出 + 提示用户重启**（用户拍板"auto-reload"）
3. **PR#8 exe 化是否本期必做** → **✅ 必做**（用户拍板"exe-now"）
4. **G5 §E.1 12 元信息的全选/全取消快捷键** → **✅ 加"全选/全不选"两个按钮**（用户拍板"g5_shortcuts"）
5. **F.1 e 多规则合并** → **✅ 按 m.rules 动态拼接**（用户拍板"f1_multi_rules"）

调整后 PR 行数预估：
- PR#1（G1+G2）：HTML 80 + JS 70
- PR#2（G3）：HTML 15 + JS 60 + PY 35
- PR#3（G4）：HTML 4 + JS 6
- PR#4（G5 + 快捷键）：HTML 75 + JS 100 + PY 40
- PR#5（G6）：HTML 30 + JS 50 + PY 15
- PR#6（G7）：HTML 3 + JS 30
- PR#7（G8 + auto-reload）：HTML 30 + JS 50 + PY 70
- PR#8（exe）：PY 100 + spec/build_exe.ps1/test_smoke_exe_entry.py

**合计**：HTML 237 + JS 366 + PY 260 + 新测试 22 个 = **863 行** + 8 个 PR。
