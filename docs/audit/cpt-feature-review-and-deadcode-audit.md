# 功能评审 + 死代码审计报告（合并版）

> 范围：①功能评审（沿用此前标准：该加/多余/重复，对照此前炒币者/研究者双视角 P0–P2 清单逐项核对）；②死代码/死模块审计（代码审计补漏）。
> 证据：AST 导入图 + vulture 静态扫描 + 前端源码逐行实读 + 运行时实证。仓库自 M7 评审以来未变（HEAD `17a346e`）。

> ⚠️ **历史快照，不代表当前状态。** 本文件写于 HEAD `17a346e`，此后仓库又走了
> **65 个提交**（截至 2026-09-25 排查时的 `bba12b2`）。其中的行号、文件清单、
> "4 个惰性控件"等细节**可能已过期**；只有"两个子系统零生产入口"这个大结论
> 经 2026-09-25 用导入图可达性复核**仍然成立**（见 `docs/pending-wiring.md`）。
> 要看当前状态请用：`docs/pending-wiring.md`（仍在维护）、
> `docs/audit/audit-20260925.json`、`docs/audit/cpt-code-audit-20260925.md`。
>
> 2026-09-25 把本文件从**仓库根目录**移入 `docs/audit/`：原先它孤零零放在根目录
> 且落后 65 个提交，极易被误当成现状（排查 P1-3）。

---

## 总体结论

1. **前端长得比后端快**：dashboard 界面已长出模式切换、三个选择器、parity/事件审计/级别树等一整套面板骨架，但其中 **4 个控件在独立页面里完全惰性**——切换后页面没有任何反应。这是当前最大的功能问题：比没有控件更伤信任。
2. **死代码本身很少，"未接线代码"很多**：真正该删的死代码只有 4 处；但有 **两个完整的子系统**（原生缠论管线 8 模块 + dashboard 服务层 20+ 模块）目前只被测试驱动、没有任何生产入口到达——它们不是死代码，却正走在变成事实死代码的路上，需要产品级决策。
3. **之前的 P0 有 3 项落地**（十字光标、收盘倒计时、涨跌幅显示），但涨跌幅语义有偏差，且缩放、MACD、24h 真实数据等核心项仍缺。

---

## 第一部分：死代码 / 死模块审计

### A. 真死代码（建议删除或修复，共 4 处）

| # | 位置 | 证据 | 处置 |
|---|------|------|------|
| A1 | `cpt/llm/`（整个包） | `__init__.py` 为空占位；全仓 grep 零引用（无 `from cpt.llm` / `import cpt.llm`） | **删除**。LLM 规划若仍在路线图上，放一句话进文档即可，空包只会误导新成员 |
| A2 | `cpt/application/dashboard_parity.py::build_parity_snapshot` | 全仓搜索**无生产调用方**（仅自身定义）；函数体恒把 oracle 传空 `()`，实证产出 `match_rate=0.0`、全部 `extra` 的假结果（M7 已指出，本次确认仍无调用方） | **修复签名或删除**。留着是陷阱：任何后来者调用它都会得到"0% 匹配"的假象 |
| A3 | `cpt/application/replay.py::_infer_interval_ms` 中的 `positive_diffs` 计算 | 计算后被完整构建，但两个分支都 `return configured_interval`——推断从未发生，与 docstring"取最小正间隔"直接矛盾（M7 中危 1，本次确认仍未修） | **修复**：`return min(positive_diffs)`，补错配回归测试 |
| A4 | `cpt/domain/containment_trace.py` 的 `"replaced"` 字面量 | `decision: Literal["contained","replaced","merged"]`，但 `trace_containment` 只产出前两者与 `merged` 中的两种；测试也只断言子集 | 收窄为 `Literal["contained","merged"]` |

### B. 未接线代码（非死代码，但需产品决策——共两组子系统）

> 判定标准：有测试覆盖、有明确设计意图，但**没有任何生产入口可达**。它们今天不是死代码；若持续不接线，明天就是。

| # | 子系统 | 规模 | 证据 | 风险 |
|---|--------|------|------|------|
| B1 | **原生缠论管线**：`domain.contain/fractal/bi/zhongshu/trend_type/recursion/signal` + `engine.rebuild/realtime` | 约 8 个模块 | 生产入口 `replay.py` 只经 `reference_chanlun` adapter 调外部后端；grep 确认 `cpt/engine` 零生产导入；原生管线被 100+ 测试（含 oracle parity）覆盖 | **双实现漂移**：adapter 后端与原生实现并行演进，parity 测试是唯一的同步约束；一旦 oracle 测试继续默认跳过，两边会悄悄分叉 |
| B2 | **dashboard 服务层**：20+ 个 `dashboard_*` 模块 + `cpt/web/app.py` + `dashboard_realtime.py` + snapshot 聚合器 | 约占 M6 后新增代码的一半 | 22 个测试文件引用 vs 仅 5 个生产文件引用（且都是服务层内部互相引用）；`pyproject.toml` **无 console_scripts**；replay CLI **无 dashboard/web 子命令**；`web/app.py` 无 `__main__`；文档记录的唯一入口是 `python -m cpt.application.replay --input/--export` | **前端已长出来，后端没接上**：界面上那些面板的数据在独立部署时无人供给——这正是第一部分功能问题"惰性控件"的根因 |
| B3 | `cpt/domain/containment_trace.py::trace_containment` | 单模块 | 仅 `tests/test_containment_trace.py` 调用 | 随 B2 的 R2 检查器一起接线 |

**结论**：B 组不该删，但需要一个"接线里程碑"。建议给每个子系统一个明确的激活点：B1 → replay CLI 增加 `--backend native`；B2 → 一个能跑起来的 `python -m cpt.web` 入口。没有激活点的"未接线代码"，半年后会退化为 A 组。

### C. 误报排除（不算死代码）

- vulture 唯一命中：`web/app.py::log_message` —— 标准库 `BaseHTTPRequestHandler` 要求的签名覆盖，**保留**；
- `scripts/compare_oracle.py`、`fetch_references.sh`：有 `__main__`/被文档引用，属正常运维脚本；
- `rust_chanlun` adapter：被 `compare_oracle.py` 导入，属 oracle 对照基建，**保留**；
- 测试文件引用的 helper（`build_parity_view` 等）：被测试使用即非死代码。

---

## 第二部分：功能评审

> 对照此前《炒币者视角 P0–P3》《研究者视角 R1–R14》逐项核对落地进度。

### A. 已实现（值得肯定）

| 之前建议 | 现状证据 |
|---------|---------|
| 十字光标 + OHLC 悬浮（炒币 P0-6） | `installCrosshair` 已实现，canvas mousemove + tooltip |
| 收盘倒计时（炒币 P0-8） | `countdown` 元素存在（line 1762） |
| 涨跌幅显示（炒币 P0-1） | `market-change` 已接线，红绿着色、方向指示 |
| 本地注释（研究 R11） | localStorage 持久化确认（lines 775/781），key 按选中结构 |
| 研究者面板骨架（R4/R7/R9 方向） | parity、事件审计、信号历史、级别树、引擎状态五个面板已就位 |
| 24h 数据通道（炒币 P0-2 的后端） | `dashboard_market_fetch.py` 可聚合 24h 并注入 snapshot；web 层有 `/api/dashboard/market-24h` 路由 |

### B. 仍缺失（之前提过、仍未做）

**炒币者视角**：
- ❌ 图表缩放（无任何 wheel/dblclick 处理）——K 线多了只能看一页；
- ❌ MACD 副图（背驰无从验证，这是缠论一买的命门）；
- ❌ 最新价标记线；
- ❌ 24h 高/低/量真实数据（通道有了，但 standalone 页面默认 `{"available": false}`——槽位仍在空转）；
- ❌ 浏览器通知/声音提醒；
- ❌ 多级别真实叠加数据。

**研究者视角**：
- ❌ R1 数据集浏览器（runs 列表面板有，但无多数据集管理）；
- ❌ R2 逐根检查器的 UI（后端 `inspect_bar` 已就绪且有测试，前端未接）；
- ❌ R5 可复现性面板的数据（reproducibility 键有路由，内容待填）；
- ❌ R8 配置对比（后端 `dashboard_config_compare.py` 就绪，前端未接）。

### C. 新发现的功能问题（本轮重点）

#### 🔴 C1. 四个控件完全惰性：切换后页面毫无反应

`symbol-select`、`level-select`、`interval-select` 三个选择器和 `mode-switch` 模式切换，点击后只 `dispatchEvent(new CustomEvent(...))`——但整个 `dashboard.js` 里**唯一监听的事件是 `cpt:realtime-updated`**（line 1543）。`cpt:symbol-changed` / `cpt:level-changed` / `cpt:interval-changed` / `cpt:mode-changed` **没有任何监听者**。

用户视角的后果：把周期从 5m 切到 1h，K 线图纹丝不动；把模式从"研究"切到"盯盘"，面板一个不少。这比没有控件更糟——它**承诺了交互然后违约**，三次之后用户不再信任界面上任何按钮。

**处置**（二选一，不要都留着）：
- 若独立页面就是 demo：把这四个控件标为 `disabled` 并加注"接入实时数据后可用"；
- 若要它们工作：让选择器变更触发重新构建/过滤当前 snapshot（前端已有全部数据时，`level-select` 至少可以纯前端过滤结构图层）。

#### 🔴 C2. `data-mode` 属性被两套语义覆写

`root.dataset.mode` 被两处写入：
- line 413：写入 `runtime.mode`（`"offline"` / `"realtime"`）——**数据模式**；
- line 1600/1605：写入 `state.mode`（`"watch"` / `"research"`）——**视图模式**。

同一个 HTML 属性，两套语义互相覆盖，谁后写谁赢。line 421 的连接提示 `mode=${root.dataset.mode}` 读到的可能是"research"而不是"offline"。且 grep 确认 `dashboard.css` **没有任何 `data-mode` 规则**——模式切换连视觉差异都没有。

**处置**：拆成两个属性——`data-runtime-mode`（offline/realtime）与 `data-view-mode`（watch/research），CSS 按后者控制面板显隐（这正是模式切换本该有的实际效果）。

#### 🟡 C3. "涨跌幅"语义偏差

`market-change` 计算的是**可视窗口涨跌幅**（`(last.close − first.open) / first.open`），但币安惯例和标签直觉都是 **24h 涨跌幅**。窗口只有几十根 K 线时，这个数会与用户在任何行情软件看到的 24h% 对不上，造成"数据错了"的错觉。

**处置**：二选一——标签改为"窗口涨跌幅"，或等 `market_24h` 通道接通后显示真实 24h%。

#### 🟡 C4. 顶栏拥挤且重复加剧

顶栏现有约 9 个 meta 项：模式、交易对、级别、周期、**切换周期**、数据来源、更新时间、结构状态，外加刷新按钮与提醒。问题：
- "周期"（只读显示）与"切换周期"（选择器）是同一信息的两种形态，并排出现；
- 模式徽章（offline）+ 数据来源（fixture）+ 底部连接状态行，三处仍在说同一件事（此前已提，本次随控件增多更显拥挤）。

**处置**：周期只保留选择器（选中值即显示）；三处状态合并为一盏模式灯（此前建议仍未执行）。

#### 🟢 C5. `realtime-alert` 占位槽位

"无新信号提醒"常驻显示，只有外部注入 `cpt:realtime-updated` 事件才会变化——离线 demo 中永远 idle。与之前 24h"—"槽位同类：**占位但永不更新的元素，不如隐藏**。

#### 🟢 C6. 本地注释的 key 脆弱

注释 localStorage key 用 `sourceIds.join(",")` 生成——结构一旦 revision 演进、source_ids 变化，旧注释即成孤儿。研究者做长期标注时会莫名"丢笔记"。**处置**：key 改用结构稳定标识（symbol+level+start_time+kind），或导出/导入注释的功能。

### D. 多余与重复（沿用此前标准复核）

- **依旧多余**：K 线根数、时间范围统计、schema 版本、规约 hint（此前已列，仍未收）；
- **新增重复**：周期显示 vs 切换周期选择器（C4）；
- **建议收进折叠区的清单不变**，此处不再重复罗列。

---

## 第三部分：产品经理观点

1. **本轮最大的信号是"前后端节奏失衡"**。前端按研究报告的蓝图长出了完整骨架，但后端服务层没有生产入口、控件没有事件回路。这不是 bug，是**里程碑排序问题**：D 阶段（接线）应该插队在所有新面板之前。否则每个新面板都在给一个不工作的界面增加表面积。
2. **惰性控件比缺功能更贵**。缺功能是"还没有"，用户会等；惰性控件是"骗了三次"，用户会走。建议立一条团队规矩：**控件合入的 Definition of Done 必须包含"操作后有可见反馈"**，哪怕反馈是 toast 提示"该功能需实时模式"。
3. **两个"未接线子系统"需要各自一个激活里程碑**，而不是继续被动等待：B1（原生管线）给 replay CLI 加 `--backend native` 即可激活，工作量小、收益大（摆脱对 GPL 参考实现的运行时依赖）；B2（dashboard 服务层）给一个 `python -m cpt.web --fixture case.json --port 8080` 的最小入口——**能跑起来的 demo 是最好的需求验证器**。
4. **死代码审计应进入 CI**：vulture（min-confidence 80）本次扫描近乎全绿，说明基线健康。建议把 vulture + 一个简单导入图脚本纳入 CI，A 组那 4 处就不会活过下一次提交。

---

## 优先行动

1. **修复或禁用 4 个惰性控件**（C1）——当前对用户伤害最大；
2. **拆 `data-mode` 双写**（C2）——半小时工作量，消除语义冲突；
3. **给 dashboard 服务层一个最小生产入口**（B2：`python -m cpt.web`）——让前端面板第一次吃到真实数据，同时一次性消灭"24h 槽位空转""提醒永远 idle"两个占位问题；
4. **清掉 4 处真死代码**（A1–A4），并把 vulture 接入 CI；
5. 之后再回到常规功能排期：缩放、MACD、24h 真实数据、R2 检查器 UI。

---

*风险提示：本报告仅针对软件产品的功能设计与代码健康度，不构成任何投资建议；加密资产市场波动剧烈、风险极高，任何指标与信号展示均不代表对未来价格的预测，使用者须独立决策并自担风险。*
