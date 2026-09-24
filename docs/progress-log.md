# CPT 进度日志

维护人：队长（DSH 会话）
本轮起算：2026-09-24（R13）
上一本日志：`docs/archive/progress-log-至R12-2026-09-24.md`（R1–R12，1067 行，含已废弃的 chanlun 参照记录）
总计划：`/home/ubuntu/work/cpt-audit/CPT-总计划-2026-09-24.md`（唯一任务台账，轮次与验收以它为准）
commit 规范：每个里程碑验收通过后一次 commit；阶段内允许 working commit

## 0. 开局盘点（R13 起点）

- 仓库：`/home/ubuntu/work/Chan_Pattern_Trader/`
- remote：`https://github.com/nxz1026/Chan_Pattern_Trader.git`，分支 `main`
- 起点 HEAD：`930051b`（R12），R1–R12 全部已推
- 测试基线：**182 passed**
- 门禁：ruff / ruff format / mypy / vulture / import-linter 全绿
- 看板：`https://127.0.0.1/cpt/`，后端 `127.0.0.1:8010`，前端 `/var/www/cpt-dashboard/`

## 1. 决策日志

### 2026-09-24 决策（本轮全部，对应总计划 §0）

- **G1** 移除 3 个参照：`chanlun==2606.73`、`chanlun-pro@78ffa470`、`chanlun_pine@0c028ef`（GPL-3.0）。理由：其分型/笔实现与缠论定义冲突，无参照价值。
- **F1** 修仓库 nginx 模板，补 `location /cpt/api/`（线上已修，仅模板漂移）。
- **F2** 删 oracle 全家桶。
- **F3** `reference_chanlun.py` 不改名（承重件，被 9 处 import）。
- **G2** `progress-log.md` 存副本后重开。
- **B** 不考虑授权，只考虑项目好不好用。
- **A1** 分型/笔 → czsc；中枢 → 留 CPT 自研但**改延伸不收紧**；走势类型/线段/递归 → 自研。
- **A2** `min_bi_len = 6`（czsc 默认；4–7 区间实测不敏感）。
- **A3** 新增 `RulesConfig.min_bi_len`（量纲=去包含后K线根数），`min_elements_for_higher_bi` 不动。
- **C1–C6** 复权因子走 Wind 后复权 / 涨跌停特殊处理 / 停牌跳过 / T+1 标注 / 非交易日不出图 / 只做日线但要合成周月线中枢。
- **D** 并入现有看板；热门池+自选；自选入口即时生效(localStorage)+落盘可选。
- **E1/E2** 四画布 flag 切换；lightweight-charts 内联 vendor 不走 CDN。
- **一买** 移植 czsc `check_first_buy`，`Bi` 加 `power_volume`。
- **未完成笔** 暂时不画。

### A1 的依据（本轮实测，决定性）

把 czsc 的笔喂进 **CPT 自己的中枢函数**：

| 方案 | 分型 | 笔 | 笔中枢 |
|---|---|---|---|
| CPT 现状 | 327/357/353 | **326/356/352** | 43/45/43 |
| czsc 笔 → CPT 中枢 | 202/203/245 | **50/49/49** | **6/6/6** |
| MIT Rust oracle | 58/62/60 | 57/61/59 | 9/8/8 |

CPT 笔端点中位跨度 = **2 根原始K线**；跨度 <4 根的笔占比 **66.9%/74.4%/73.0%** —— 两根 3K 分型窗口重叠，**按定义不可能成笔**。换笔后中枢自动回到 6/6/6。

中枢延伸规则的实测：CPT 现状**延伸时单调收紧**（`zhongshu.py:108-112`），偏离缠论原文，把一个大中枢切成 6 个并切出宽度 4.5 的畸形中枢；改为**不收紧**后与 czsc 中枢**逐项完全一致**（fixture1 区间宽 `[151.0, 66.5, 17.2]`、纳入笔数 `[38, 6, 5]`）。

## 2. 轮次记录

### R13 — 清账

状态：**已完工**（验收见下）

改动：

1. **去参照（G1）**
   - `pyproject.toml`：删 `oracle = ["chanlun==2606.73"]`
   - `scripts/fetch_references.sh`：REPOS 由 3 个缠论参照改为 `czsc@701e480a`（Apache-2.0）+ `wbt@39bb1e8a`（MIT）
   - `.gitignore`：3 行 chanlun 参照改为 `/references/czsc/`、`/references/wbt/`
   - `references/README.md`：重写参考表，记录已移除的参照与理由
   - 删本地 `references/{chanlun-pro,chanlun.py,chanlun_pine}`（392M）
2. **修 nginx 模板（F1）**
   - `deploy/nginx/cpt-dashboard.conf`：补 `location /cpt/api/ { proxy_pass http://127.0.0.1:8010/api/; }`（含注释说明最长前缀匹配）
3. **删 oracle（F2）**
   - `git rm`：`cpt/adapters/rust_chanlun.py`（311 行）、`scripts/compare_oracle.py`（382 行）、`tests/test_compare_oracle.py`（40 行）、`docs/m2-oracle-diagnostic.md`（52 行）
   - `.github/workflows/ci.yml`：去掉 `oracle-parity` job；ruff/format/vulture 命令去掉 `scripts/compare_oracle.py`
4. **重置日志（G2）**
   - `docs/archive/progress-log-至R12-2026-09-24.md`（1067 行）存档；本文件从 R13 起
5. **清失效引用**
   - `docs/rules.md:8`：去掉 chanlun-pro/chanlun.py/Pine 三处实现参照，改为 czsc

**未改**：`docs/m6-quality-report.md`、`docs/dashboard-final-acceptance.md`、`docs/low-risk-hardening.md`、`cpt-feature-review-and-deadcode-audit.md` —— 它们是带日期的历史验收/审计快照，改它们等于篡改历史记录。其中命令已失效，以本日志和总计划为准。
