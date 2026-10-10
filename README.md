# qc-tool-

临床检验室内质控工具（Spec V1.3）—— 单人本地 Windows 桌面 .exe。

## 状态

| 功能 | 状态 | 分支 / PR |
| --- | --- | --- |
| **A** — 检验项目 + 检测指标 + 实验室名称（业务层 + 测试） | ✅ done | `1-issue-a...` |
| B — 数据录入与导入 | ⬜ | — |
| C — 统计量自动计算 | ⬜ | — |
| D — Westgard 规则自动判定（issue #4） | ✅ done | `westgard-issue4` |
| E — 质控图绘制 | ⬜ | — |
| F — 失控排查方向提示 | ⬜ | — |
| G — 报表与导出 | ⬜ | — |
| **db / init / backup**（spec §8.1 / §8.2） | ✅ done | `feat/db-init-and-backup` |

## 模块清单

- `qc_tool/db.py` — SQLite schema（spec §8.1 一字不差）
- `qc_tool/init_db.py` — 建库 CLI（`python -m qc_tool.init_db`）
- `qc_tool/backup.py` — 备份策略（spec §8.2）
- `qc_tool/westgard.py` — Westgard 多规则判定（功能 D）
- `qc_tool/projects.py` — 项目 / 检测指标 / 实验室名称业务（功能 A）

## 测试

```sh
python -m pytest -v
```

最新一次全量：**119 passed, 1 skipped in ~1s**
- db：10
- backup：14
- westgard：53
- projects：42（1 skip 是 §A.2 d 图表配置 UI 项，留待功能 E）

## 规范

详见项目根目录 `临床检验室内质控工具 → 规格说明书（Spec V1.3）.md`。
