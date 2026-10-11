# qc-tool-

临床检验室内质控工具（Spec V1.3）—— 单人本地 Windows 桌面 .exe。

## 状态

| 功能 | 状态 | 分支 / PR |
| --- | --- | --- |
| **A** — 检验项目 + 检测指标 + 实验室名称（业务层 + 测试） | ✅ done | `1-issue-a...` |
| B — 数据录入与导入 | ⬜ | — |
| C — 统计量自动计算 | ⬜ | — |
| D — Westgard 规则自动判定（issue #4） | ✅ done | `westgard-issue4` |
| **E** — Levey–Jennings 质控图（issue #5） | ✅ done | `issue-5-lj-chart` |
| **G** — 报表与导出（issue #7） | ✅ done | `7-issue-g-月度质控报表与失控记录导出` |
| F — 失控排查方向提示 | ⬜ | — |
| G — 报表与导出 | ⬜ | — |
| **db / init / backup**（spec §8.1 / §8.2） | ✅ done | `feat/db-init-and-backup` |

## 模块清单

- `qc_tool/db.py` — SQLite schema（spec §8.1 一字不差）
- `qc_tool/init_db.py` — 建库 CLI（`python -m qc_tool.init_db`）
- `qc_tool/backup.py` — 备份策略（spec §8.2）
- `qc_tool/westgard.py` — Westgard 多规则判定（功能 D）
- `qc_tool/projects.py` — 项目 / 检测指标 / 实验室名称业务（功能 A）
- `qc_tool/lj_chart.py` — Levey–Jennings 质控图业务层（功能 E，含 G.3 PNG 导出）
- `qc_tool/ui_lj_chart.py` — 质控图 PyQt6 对话框（功能 E）
- `qc_tool/reports.py` — 月度质控报表 + 失控记录 xlsx/PDF 导出（功能 G 业务层）
- `qc_tool/ui_reports.py` — 月报 / 失控记录导出 PyQt6 对话框（功能 G UI）

## 功能 E 使用方法

启动主程序后，菜单 `系统 → 质控图…` 打开质控图对话框。
顶部下拉按 4 维（检测指标 + 水平 + 批号 + 月份）过滤，切换即重绘。

- `<` / `>` 按钮：上 / 下月翻页。
- `图表配置`：12 个字段（项目名 / 检测指标 / 简称 / 单位 / 厂家 / 批号 / 水平 / 靶值 / SD / 操作人员 / 前处理人员 / 时间区间）可独立显隐；`时间区间` 始终保留作为兜底。
- `导出图片`：PNG / JPG 导出当前图（DPI 默认 300，元信息显隐由"图表配置"决定）。

## 功能 G 使用方法

启动主程序后，菜单 `报表` 提供两个入口：

- **生成月报…**：选项目 + 月份 → 弹出"选择导出指标"对话框（每个「检测指标 +
  水平」为一个可勾选指标）→ 至少勾选 1 个 → 保存为多 sheet `.xlsx`。
  每个勾选指标生成 3 个 sheet：
    - `本月原始` —— 全部测定值 + 状态
    - `累计统计` —— 检测指标建立日 ~ 上一自然月最后一天
    - `去失控累计` —— 排除"失控"与"作废"后重算
- **失控记录导出…**：选项目 + 时间段（本月 / 上月 / 今日 快捷按钮）→
  选 Excel 或 PDF → 保存。xlsx 全字段（含触发日期 / 规则 / 处理过程 /
  已检查项 / 处理人 / 审核人 / 审核备注 / 样本批次 / 样本编号 / 误差类型等）；
  PDF 矢量（DPI 关闭流压缩；字号 ≥ 10pt）。

边界提示：月内无数据 → "无数据"；未勾选任何指标 → "请至少勾选一个指标"；
时间段无失控 → "该时间段无失控记录"。

## 测试

```sh
python -m pytest -v
```

最新一次全量：**297 passed, 3 skipped in ~25s**
- db：10
- backup：14
- westgard：53
- projects：42（1 skip 是 §A.2 d 图表配置 UI 项，留待功能 E — 现已并入 E 完成）
- measurements / F1 / B2 import：~80
- statistics：~30
- lj_chart（issue #5 业务层）：23
- lj_chart UI（issue #5 UI）：10

## 规范

详见项目根目录 `临床检验室内质控工具 → 规格说明书（Spec V1.3）.md`。
