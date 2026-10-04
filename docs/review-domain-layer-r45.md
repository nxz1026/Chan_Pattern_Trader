# 复盘：`cpt/domain/` 层（R45）

> 16 文件 / 2,922 行。**缠论算法核心** —— 算错就是信号算错，
> 是全仓**爆炸半径最大**的一层。
>
> 结论：**未发现需要修改的问题**。这层是 R45 复盘六层以来第**二**个干净结果。

---

## 1. 这层的失效模式与其他层不同

| 层 | 典型失效 | 防御 |
|---|---|---|
| `storage` / `adapters` / `application` / `web` | 静默降级、把故障说成别的事 | 异常语义 + 门禁 |
| **`domain/`** | **算得出结果，但结果是错的** | **前提正确** + 语义契约 |

实测数据：**16 个文件里只有 2 个 `except`**，唯一的静默分支是
`structure_events._as_opt_int`（纯类型转换器，行为正确）。

这是**正确的设计** —— 纯算法层「算不出就该炸，不该静默出一个错的结构」。
但代价是：**这一层唯一的防线是前提正确**，所以 R30 才会建那个
看起来很奇怪的「文档断言」门禁（`tests/test_domain_semantic_contract.py`）。

## 2. 冻结口径的文档 ↔ 代码一致性（逐条核对）

`config.py` 有四个参数自称「§9.x 冻结」，逐条与 `docs/rules.md` 比对：

| 参数 | 代码 | rules.md | 一致 |
|---|---|---|---|
| `zs_wzgx = "zgd"` | ✅ | `:479` 同 | ✅ |
| `divergence_compare = "area"` | ✅ | §9.6 | ✅ |
| `min_elements_for_higher_bi = 5` | ✅ | `:488` 量纲说明一致 | ✅ |
| `min_bi_len = 6`（量纲=去包含后 K 线根数） | ✅ | `:136`/`:494` 一致 | ✅ |

尤其注意 `min_bi_len` 与 `min_elements_for_higher_bi` **量纲不同**、
`rules.md:488` 专门写了「不可混用」—— 这正是容易写错的地方，写对了。

## 3. 真机不变量验证（不是重跑实现，是查「读代码看不出来」的性质）

### ① 递归映射：保序 + 区间不真重叠

喂 5 个走势类型，覆盖三种边界：

| 输入 | 期望 | 实测 |
|---|---|---|
| `[0,100] dir=1` | 保留 | ✅ 保留 |
| `[100,200] dir=-1` | 端点相接 ⇒ 保留 | ✅ 保留 |
| `[150,250] dir=1` | 与上一个**真重叠** ⇒ 跳过 | ✅ 跳过 |
| `[250,300] dir=0` | 方向未定 ⇒ 不产出 | ✅ 跳过 |
| `[300,400] dir=1` | 保留 | ✅ 保留 |

产出 3 个元素，**与 `recursion.py` 模块 docstring 承诺的 1~4 条完全一致**。

### ② 中枢不变量

7 笔纯交替（无三笔重叠）⇒ 产出 **0 个中枢**。这是**正确**的
（`build_zhongshus` 要求不足三笔返回空元组），不是 bug。

### ③ 级别标签（R28-9 的直接回归）

```
a_share  level=5   → '日线级别'            ✅
a_share  level=30  → '日线之上的高级别'      ✅
crypto   level=5   → '5 分钟级别'          ✅
crypto   level=30  → '30 分钟级别'         ✅
```

R28-9 那个「把 A 股日线结构讲成 5 分钟级别」的 bug **没有复发**。

## 4. 一个观察（不修，仅记录）

R30 的「文档断言」门禁只有 **5 个用例**，只盯 `levels` 单位这一件事。
而这层**同类风险**还有别处 —— 任何「把口径写进 docstring」的地方，
写错了都没有运行时症状。同类的还有 `docs/rules.md` 本身（若与代码漂移，
LLM 读到的就是错的）。

**不建议**现在加更多文档断言：门禁的价值来自「覆盖了真的会出错的点」，
而不是数量。R45 未发现其他漂移，**先记着**，等真的发现一处再加一条。

## 5. 测试

R30 立的 `tests/test_domain_semantic_contract.py`（5 个用例）仍在，
本轮未新增测试 —— 因为**没找到需要新增覆盖的缺陷**。

---

## 5. 补：R45 第二轮 —— 「冻结」到底护住了什么

owner 指示「先解开冻结，对齐功能 + 修 bug」。逐条查了仓里两处**契约级**冻结
（`@dataclass(frozen=True)` 是不可变数据类，属另一回事，不算），
结论是：**冻结没有挡住任何修复，但它的声明高估了自己的保护范围。**

### 两处契约冻结的现状

| 契约 | 声称 | 核查结果 |
|---|---|---|
| `RulesConfig` v0 | 「与 `docs/rules.md` §9 冻结，改动走 v0.x 流程」 | 四个参数与 rules.md **逐条一致**；`SCHEMA_VERSION="v0"` 硬编码 |
| export schema v1 | 「冻结，三方契约」 | `schema_version` 前后端一致（都是 `dashboard.v2`）；export 自身是 `v1` |

### 但「冻结」实际由**三套互不交叉**的机制分担

| 机制 | 守什么 | 谁在读 |
|---|---|---|
| `SCHEMA_VERSION` | **回放 fixture** 反序列化时拒收异版本 | 仅 `from_dict`（`replay.py:126`） |
| `reproducibility.config_hash` | 运行历史的「换参数了」比对 | `run_metric.py:94` **确实在比对** |
| `reproducibility.rules_version` | 快照元数据对外展示 | 前端 |

### 两个实测发现（已写进 `config.py` docstring）

1. **在线快照的顶层 `config` 是空的**（`None`）。`config` 只由
   `empty_ashong_snapshot` 填，**真实有数据的路径不填**。所以
   `config_version` 根本不在线上快照里，它只在回放这条路上有意义。
2. **`SCHEMA_VERSION` 永远不会变** —— 硬编码 `"v0"`，且 `from_dict` 会
   `pop` 掉传入的同名字段。于是「改参数必须升版本」这条纪律**没有任何机制强制**：
   改了参数、版本号还是 `v0`，旧 fixture 照样通过校验被回放，无任何提示。

### 因此 R45 的动作是「**把冻结说准**」，不是拆掉它

**没有证据支持解除任何一处冻结** —— 真正检测漂移的 `config_hash` 在正常工作，
拆掉 `from_dict` 的守卫只会**减少**安全性。改成把声明改准确：

> `config_version` 是**回放入口的单点守卫**，不是全局版本闸门。
> 改参数时**必须手工**同步 `config_version`，否则回放静默用旧口径。

这与 web 层那三个幽灵接口是同一类问题（**声明比实际强**），
也与 R30 立文档断言门禁的动机一致。

### 一个建议（未做，需要 owner 决定）

`config_version` 恒为 `"v0"` 让这道守卫**永远不会触发**。若要让「升版本」这条
纪律真正被强制，可选：

- 从 `RulesConfig` 的**字段与取值**派生一个版本（改任一参数即变）——
  改动小，但会让 fixture 因参数微调而全部失效；
- 或保持现状，改为在 CI 里断言「改了 `RulesConfig` 字段就必须同时改
  `SCHEMA_VERSION` 或 `docs/rules.md` §9」。

**两者都有代价，属产品决策，本轮不动。**

---

## 6. 补：方案 (b) 落地 —— 冻结变更门禁

owner 选定 (b)：CI 断言「改了口径参数就必须同时升版本 + 同步 `docs/rules.md` §9」。

### 为什么不能写成「和上次提交比 diff」

CI 是**全新 checkout，没有 git history**，任何依赖「上一次是什么」的判断都跑不了。
所以做成**自包含的钉子**：把 14 个字段与默认值钉在
`tests/test_domain_frozen_contract.py::FROZEN_V0`，改了字段/默认值就红。

### 这道门禁当场逮到一个真文档缺陷

首次运行就红：

    这些冻结参数在 docs/rules.md 里查不到：
    ['contain_direction', 'zs_level_count', 'macd_fast', 'macd_slow',
     'macd_signal', 'divergence_compare', 'levels']

**7 个冻结参数从未被记录进规则文档** —— 而 `config.py` 明写「字段语义详见
`docs/rules.md` §9」。这与 web 层那三个幽灵接口、注释与代码不一致，
是同一类问题：**声明比实际强**。

已补 `docs/rules.md` §9.9（全字段一览）/ §9.10（`levels` 单位随市场而异）/
§9.11（`config_version` 守的是回放入口）。

### 门禁的四条

| 用例 | 拦什么 |
|---|---|
| `test_frozen_config_matches_pinned_snapshot` | 改了字段/默认值却没走升级流程（报错里写清三步） |
| `test_every_frozen_param_is_documented_in_rules_md` | **新增参数忘了写进规则文档**（本次就靠它逮到 7 个） |
| `test_schema_version_is_pinned_and_matches_snapshot` | `SCHEMA_VERSION` 与钉子漂移（否则回放守卫永远误伤或永远失效） |
| `test_config_version_cannot_be_overridden_by_data` | fixture 自称 `v99` 绕过 v0 契约 |

第 2 条是关键：**「同步文档」也不靠自觉，而是被机械检查**。

---

## 7. 补：chanlun 三后端对比（R45 真机做成了）

`backend_factory` 记着一条真陷阱：`auto` 模式一旦发现装了 czsc 就会
**静默切后端**，「看板上每一根笔都会变」。R45 把它**实测**了。

⚠️ 关键的一步：**czsc 是在 Oracle 上装上的**（沙箱装不上 —— PyPI 对 pip
重置、crates.io 403、czsc 是 Rust 扩展需 cargo）。我第一轮只在沙箱试安装就
下了「做不成」的结论，**下早了**。

装它**不改变生产行为**：`DEFAULT_BACKEND` 硬编码为 `native`，
不依赖 czsc 是否安装（`auto` 的隐患早被 R16-4 堵住）。
实测服务进程环境里没有 `CPT_CHANLUN_BACKEND` ⇒ 走 `native`。

### 结果：native 与 czsc 的**笔差 3.4~5.7 倍**（6 只票，无一例外）

| 票 | native 分型/笔/中枢 | czsc 分型/笔/中枢 | 笔倍数 |
|---|---|---|---:|
| 600519 | 37 / 36 / 6 | 36 / 8 / 1 | 4.5× |
| 000001 | 35 / 34 / 5 | 24 / 6 / 1 | 5.7× |
| 000002 | 34 / 33 / 3 | 31 / 9 / 2 | 3.7× |
| 300750 | 46 / 45 / 6 | 43 / 11 / 2 | 4.1× |
| 601398 | 38 / 37 / 5 | 37 / 11 / 2 | 3.4× |
| 002294 | 37 / 36 / 5 | 22 / 8 / 1 | 4.5× |

**分型数接近（差 1~15），笔数差 3.4~5.7 倍，中枢差 2~6 倍。**
`backend_factory` 文档里那句「每一根笔都会变」被**实测证实**。

两边**契约检查都全通过**（笔严格递增、无真重叠、方向 ±1；中枢 `high>low`、
`bi_ids` 不越界）⇒ **不是谁算错了，是口径不同**。native 的笔更细
（`min_bi_len=6` 取的是去包含后 K 线根数），czsc 的笔更粗。

⇒ 这**正面回答**了 R16-4 把默认改成 `native` 的决定：切到 czsc 不会只是
「换个实现」，而是换一套结构划分。默认必须留在 native。

### 工具

`scripts/compare_chanlun_backends.py` —— 探测后端可用性、对可用的跑契约、
**明确报出缺哪个**，不假装做了对比。第三个后端（`InMemoryChanlunBackend`）是
仓内**测试占位**，脚本刻意不把它算进「三方对比」，并解释计数差异**不代表**
后端不可互换。

---

## 8. 补：`min_bi_len` 根本没作用在生产后端上（R45 实测，**未修**）

起因是三后端对比里那个反常：**配置写 `min_bi_len=6`，native 的笔中位只有 3 根。**

### 根因

``min_bi_len`` **只喂给 czsc**：

    cpt/adapters/backend_factory.py:133  if min_bi_len is None: ... CzscChanlunBackend(min_bi_len=...)
    cpt/adapters/czsc_chanlun.py:212    czsc.CZSC(..., min_bi_len=self._min_bi_len)
    cpt/adapters/native_chanlun.py      min_bi_len 出现 0 次

而 4 个调用点（``a_share_snapshot.py:284``、``web/__main__.py:260/540``、
``web/a_share.py:63``）都写成 ``resolve_backend(DEFAULT_BACKEND, min_bi_len=RulesConfig().min_bi_len)`` ——
``DEFAULT_BACKEND`` 是 ``native``，于是这个参数**被静默丢弃**。

而 ``cpt/domain/bi.py:101-102`` 明说 native **有意不做**最小跨度：

    只做端点交替与同类极端保留：**不做最小跨度 /「至少 5 根K线」门槛**

所以**代码内部是自洽的**（native 就是没有门槛），不一致的是**声明**：
``RulesConfig.min_bi_len`` 的 docstring 写「底层笔最少跨度，量纲＝去包含后 K
线根数」，读起来像一条**全局口径**，实际只是 czsc 的适配参数。典型的
「声明比实际强」。

### 这对准确性有实质影响（实测 40 只票）

| 笔跨度（含两端） | native 笔数 | 占比 |
|---|---:|---:|
| 2 根（两分型共用一根 K 线） | 240 | **17.6%** |
| 3 根 | 240 | 17.6% |
| 4 根 | 320 | 23.5% |
| **退化笔合计（<4 根）** | **480** | **35.3%** |

czsc 同一批输入：退化笔 **0%**（最短 10 根）。且 **40 只票每一只**的
退化笔占比都 > 30%。

**为什么这不只是"配置没生效"**：缠论标准里，笔要求顶底分型之间**至少有 1 根
独立 K 线**（跨度 ≥ 4 根）。跨度 2 根的分型对**中间没有可回撤空间**，
而力度度量（``power_price`` = 两端分型价格差、``power_volume``、``length``）
正是**一买/一卖背驰比较的输入**（``divergence_compare="area"`` 用 MACD 柱面积）。
笔太细 ⇒ 力度碎在 2 根 K 线上 ⇒ 背驰判定的分母不稳。

### 为什么 R45 **没有直接修**

把门槛接到 native 会**改动全部结构**：现有 41 条信号、`cpt_run_metric` 的
水位/指纹历史、缓存的 run 全部作废。而且那等于把生产口径**向 czsc 靠拢** ——
而 R16-4 恰恰是**刻意**把默认留在 native 的。

⇒ 这是一个**口径决策**，不是能顺手修的 bug。两条路：

- **A. 接上门槛**（native 也要求 ≥4 根）：结构变干净，但**所有历史作废**，
  且等于部分倒向 czsc 的笔划分；
- **B. 保持现状 + 把声明改准**（R45 已做的）：``RulesConfig.min_bi_len``
  的 docstring 明确写成「**仅 czsc 后端生效**，native 按 ``bi.py`` 的
  「不做最小跨度」执行」，并加门禁防止有人再以为它是全局口径。

**需要 owner 决定 A 还是 B。** B 是本轮已做的默认值；A 的代价很大，
但如果目标是「严格符合缠论标准笔定义」，那 A 才是对的。

---

## 追加：`reference` 后端落地（R45）

### 之前的状态

参照侧**不是一个后端**：

- `BACKEND_CHOICES` 只有 `("auto", "czsc", "native")`；
- 它的实现私藏在 `cpt/application/parity_reference.py` 的两个私有函数
  `_czsc_structures` / `_tencent_structures` 里；
- 仓内唯一的"第三个实现"是 `InMemoryChanlunBackend` ——
  **测试占位**，只做"bar 局部极值 + 相邻异类连线"，不追求真实缠论精度。

⇒ 想回答「参照侧到底是哪套算法、跑了哪份数据、为什么降级」，
只能去读 application 层的私有代码；想把它**当成后端跑一遍**（离线导出做人工比对）
根本做不到。

### 落地

新增 `cpt/adapters/reference_backend.py`（一等后端），并把
`parity_reference.build_parity_snapshot_for` 改为**委派**给它
—— 参照侧从此只有**一份**实现，不会出现「parity 走 czsc、离线导出走腾讯」的漂移。

| 项 | 值 |
|---|---|
| 回落链 | czsc（实现对照）→ 腾讯 hfq（数据链路对照）→ 抛 `ReferenceUnavailableError` |
| 用了哪一级 | `backend.source` / `backend.detail` **如实报告**（此前只有 application 层知道） |
| 默认档 | **不变**，仍是 `native` |

**刻意不做的事**：参照侧**不静默回落 native** ——
参照侧回落到生产侧等于**自己跟自己比**，那是对照面板最没意义的一种「通过」。
宁可报「没参照」。

**新异常 `ReferenceUnavailableError`** 与 `UnknownBackendError` 分开：
后者是「**档位名写错了**」，前者是「**名字对、但环境不支持**」。
混成一个，会让「配置错误」和「依赖缺失」在日志里长得一样。

### 一处设计上的真问题（实测撞出来的）

`ChanlunBackend` 契约只传 `bars`，而腾讯那条路需要**标的代码** ——
但 **`CanonicalBar` 根本没有 `code` 字段**（实测
`CanonicalBar.__init__() got an unexpected keyword argument 'code'`）。

⇒ 代码只能作为**后端级属性**显式传：`ReferenceChanlunBackend(code="600519")`，
工厂侧 `resolve_backend("reference", code=...)`。
留 `None` 时走到腾讯那级直接判不可用（**不猜代码**）——
与 `factor_from_actions`「宁可少一个台阶，也不用猜的值」同一个原则。

### 真机验证（2026-10-04，Oracle）

```
取到 300 根 K 线（600519）
✓ 参照结构算出来了
  source = czsc | detail = czsc 实现对照
  分型 99 / 笔 21 / 中枢 2
```

### 一处**语义分歧**的处理（不是 bug）

翻转 `not actions` 那支时，撞到 R44 刻意立的守卫
`test_no_actions_at_all_still_reports_no_record`
—— R44 整套测试就是为「窗口内无除权该写、查无记录该报」这个**区分**存在的。

**没有为了变绿就删断言。** 做法是：保留它的价值，改成钉住新意图 ——
断言「恒定值取自 `anchor`（199.4）而不是 1.0」。
这正是「正确写入」与「把接口故障洗成数据」的分界线。

并补了一条**对照**测试：K 线不足 30 根仍必须拒写。
两条并置说明分界线是「**有没有确定答案**」，而不是「东财说不说」。

### 回归

全量 `pytest tests/` = **2 个失败，与基线逐条相同**。

---

## 追加：R45 自己引入的 3 个生产 bug，被补测试当场抓住（R45 第五轮）

第五轮代码侧全量扫描（孤儿模块 / 零引用符号 / 零测试覆盖）撞见
**`cpt/application/parity_reference.py` 零测试覆盖** —— 而它**正是 R45 当天
刚被我重构过的**。补测试后连续炸出 3 个真 bug。

### 1. `min_bi_len` 塞错了地方 → 每张 A 股快照都抛

```python
backend.compute_structures(bars, ReferenceChanlunConfig(min_bi_len=cfg.min_bi_len))
# TypeError: ReferenceChanlunConfig.__init__() got an unexpected keyword argument
```

`ReferenceChanlunConfig` 只有 `use_fx_*` / `zs_wzgx` / `macd_*` / `fixed_commit`。
而 `TypeError` **不在** `except ReferenceUnavailableError` 里 ⇒ 直接穿出去。

**修**：`min_bi_len` 是**后端级**参数（与 `code` 同源 —— 契约只传 `bars`），
改走 `resolve_backend("reference", min_bi_len=...)`。

### 2. 引用了被重构掉的局部变量 `ref` → `NameError`

函数末尾仍写着 `ref_fractals=ref[0]` —— `ref` 是旧实现里的局部变量，
委派之后已经不存在了。

### 3. 硬转 `ChanlunResult` 丢掉了时间锚点

`BiRaw` / `ZsRaw` 存的是 **bar 索引**，parity 层要的是
`start_time` / `end_time`（毫秒时间戳）—— **两者量纲不同**。
我第一版硬转，把 `start_bar` 全设成 0 ⇒ 面板「点选高亮到对应位置」会废。

**修**：后端另开 `compute_domain_structures()` 返回**领域对象三元组**，
parity 层沿用原来的 `_normalize`（它同时吃 dataclass 和 dict）。

## 既存的：docstring 承诺「永不抛异常」，代码只接一种异常

```python
"""算出 A 股快照的 parity 块（**永不抛异常**）。"""
...
except ReferenceUnavailableError as exc:   # ← 只接这一种
```

后端抛任何别的异常（czsc 内部炸、腾讯超时、字段缺失）都会**穿出去**，
把整张 A 股快照带崩。**这句话从来没被兑现过，也从来没被测过。**

**为什么这里该宽泛捕获，而 storage 层坚持「失败必须抛」——不矛盾**：

| | 返回值的地位 | 失败该怎么办 |
|---|---|---|
| storage 的因子/信号 | **就是业务事实** | 不能伪装成空，必须抛 |
| parity 块 | **可选的交叉验证** | 失败不该带崩主流程，但**必须如实报告** |

所以宽泛捕获 + `available=False` + **写明异常类型与消息**，
而不是静默吞掉。

## 最重要的教训：我又漏了「改完要真机验证」

**这三个 bug 里，#1 会让每次 A 股快照都抛。**

而我今天**测过 parity 面板**（czsc 标签、四色配色、27/55/17 计数）——
**但那是在这次重构之前**。改完我直接提交了，**没再打开面板看一眼**。

⇒ 与今天早些时候「画布 D 塞了独立 `<script>` 就上线」是**同一个错**：
**把「我验证过上一个版本」当成了「我验证过这个版本」**。

补测试之所以有价值，不在于新测试本身，
而在于它**强制重新走一遍那条路径** —— 哪怕是离线跑，
也会立刻炸在这三处。

## 验证

- 补的 16 个测试全过（parity 降级 5 + feishu 6 + prune 5）
- **真机 snapshot**：`available: True`、`source: czsc`、
  47 分型 / 42 笔 / 7 中枢，逐条对上
- 全量 `pytest tests/` = **2 个失败，与基线逐条相同**
