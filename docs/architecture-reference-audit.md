# 参考仓库代码架构摸底

审阅基线：

- `references/chanlun-pro`：`78ffa470`
- `references/chanlun.py`：`2e4fa135`
- `references/chanlun_pine`：`0c028ef`

## 1. 结论摘要

三个仓库的架构取向不同：

| 仓库 | 主要架构特征 | 可借鉴点 | 主要问题 |
|---|---|---|---|
| `chanlun-pro` | 大型 Python 单体，算法、行情、策略、回测、Web、交易均在同一仓库 | 模块目录、交易所适配、结构对象、策略接口、回测入口 | 依赖过重；核心 `cl.py` 被 PyArmor 保护；业务边界较宽 |
| `chanlun.py` | 单文件/少文件核心引擎，`main.py` 承担 FastAPI、WebSocket、页面和数据编排 | “变动即重建”、逐层结构流水线、多级观察者、MCP 接口 | `main.py` 过大；核心对象和服务耦合；数据源与应用层混杂 |
| `chanlun_pine` | Pine 单文件脚本，类型、算法、信号、绘图全部在一个文件 | 类型先行、状态结构、实时重建、信号状态对象 | 不适合作为服务端架构；单周期限制；算法与绘图紧耦合 |

## 2. chanlun-pro 分层

### 2.1 目录分层

```text
src/chanlun/
├── cl.py                         # 核心计算实现（本版本受 PyArmor 保护）
├── cl_interface.py               # 配置、结构对象、接口和数据对象方法
├── cl_analyse.py                 # 多级别分析辅助
├── cl_utils.py / utils.py        # 工具和计算辅助
├── kcharts.py                    # K线图表/数据转换辅助
├── exchange/                     # 行情、交易所、周期转换、缓存
├── strategy/                     # 策略实现
├── backtesting/                  # 回测、K线生成、交易模拟、优化
├── trader/                       # 实盘/交易接入
├── monitor.py                    # 监控和信号提醒
├── xuangu/ / zixuan.py           # 选股、自选
├── tools/                        # AI、技能接口、工具
└── db.py / file_db.py            # 数据库和文件存储

web/chanlun_chart/
└── Web 展示、路由、任务、图表服务
```

### 2.2 实际数据流

```text
Exchange / ExchangeDB
        ↓
原始 DataFrame K线
        ↓
cl_interface.ICL / 结构对象接口
        ↓
cl.py 核心计算
        ↓
分型、笔、线段、中枢、背驰、买卖点
        ↓
strategy / monitor / backtesting / trader
        ↓
Web 图表、消息提醒、交易或回测结果
```

### 2.3 特点

1. **接口对象与核心实现分离**：`cl_interface.py` 暴露配置枚举、结构对象和查询接口，`cl.py` 执行核心计算。
2. **市场适配较完整**：`exchange/` 内有 Binance、现货、期货及其他市场适配器，统一通过 `Exchange` 抽象获取行情。
3. **策略直接依赖结构对象**：策略可以调用 `get_bis()`、`get_bi_zss()`、`line_mmds()` 等方法。
4. **应用边界较宽**：同一个仓库同时包含行情、结构计算、策略、回测、实盘、选股、Web 和 AI 工具。
5. **多级别主要是查询/辅助分析**：`cl_analyse.py` 根据高级别线查询其内部低级别线和中枢，不等同于 CPT 的走势类型递归映射。
6. **核心算法不可完全审计**：`cl.py` 被 PyArmor 保护，接口可读但核心执行细节无法逐行复核。

### 2.4 CPT 可借鉴和不应照搬

可借鉴：

```text
配置枚举与结构对象分离
Exchange 抽象和 Binance 适配器
结构对象提供只读查询方法
策略/回测通过结构接口消费结果
```

不应照搬：

```text
把所有市场和交易功能放进首版
把结构计算、策略、Web、实盘放在同一应用层
依赖加密核心作为唯一规则源
把低级别辅助查询当作递归结构引擎
```

## 3. chanlun.py 分层

### 3.1 文件分层

```text
chan.py
└── 核心缠论对象、观察者、配置、结构流水线、信号和多级别分析

main.py
└── FastAPI、WebSocket、HTTP接口、页面编排、数据请求和会话管理

signals.py
└── 信号配置/信号辅助

strategies.py
└── 策略和回测相关逻辑

chanlun_mcp.py
└── MCP 工具层，调用 chan.py 的观察者和分析器

datafeeds/
└── 前端数据源适配
charting_library/、templates/
└── 前端图表和页面资源
```

### 3.2 核心流水线

```text
原始K线
  ↓
缠论K线包含合并
  ↓
三元素分型
  ↓
笔（默认至少5根缠论K线）
  ↓
笔中枢 / 线段 / 扩展线段
  ↓
更高层结构和买卖点
```

其技术文档明确采用“变动即重建”：底层结构失效时，弹出受影响的高层结构，再从断点重建。这与 CPT 的“未确认结构允许重构、事件追加保存”方向一致。

### 3.3 特点

- 核心对象使用中文类名，概念表达直观；
- 观察者负责逐根接收 K 线并维护结构状态；
- 结构对象之间存在高层引用低层的关系；
- MCP 层可以把核心分析能力暴露为无状态工具和会话状态工具；
- Web 服务层与核心引擎耦合较重，`main.py` 体量较大。

## 4. chanlun_pine 分层

Pine 仓库只有一个主要脚本：

```text
chanlun_core.pine
├── enum 和用户自定义类型
├── 输入参数
├── K线包含处理
├── 分型识别
├── 笔构建
├── 中枢识别
├── 背驰检测
├── B1/B2/B3 信号
├── 预判和止损
├── 全量重建状态
└── plot/label/box 图形输出
```

它把算法和展示全部放在一个脚本中，适合 TradingView 指标，不适合 CPT 服务端。它的优点是每个结构都显式定义为 Pine UDT，例如 `MergedBar`、`Fractal`、`Stroke`、`ZhongShu`、`TradingSignal`、`Prediction`。

## 5. 三个仓库的共同分层

三者都隐含相同的基本管线：

```text
行情输入
  ↓
K线标准化/包含处理
  ↓
分型
  ↓
笔
  ↓
中枢
  ↓
背驰/买卖点
  ↓
展示、策略或外部接口
```

差异在于：

- `chanlun-pro` 把每层能力拆成多个 Python 包，并扩展到交易系统；
- `chanlun.py` 把大部分核心压在 `chan.py`，通过观察者增量重建；
- `chanlun_pine` 把所有内容压缩进单文件并直接绘图。

## 6. CPT 架构建议

CPT 不复制任何一个仓库的整体结构，建议采用“核心纯计算 + 适配层 + 应用层”的三层边界：

```text
src/cpt/
├── domain/                       # 纯领域模型，不依赖交易所和Web
│   ├── market.py                 # OHLCV、时间、缺口、周期
│   ├── chan_bar.py               # 缠论K线与包含处理
│   ├── fractal.py                # 分型
│   ├── bi.py                     # 新笔
│   ├── zhongshu.py               # 笔中枢和 level/zs_wzgx
│   ├── trend_type.py             # CPT 走势类型
│   ├── recursion.py               # 走势类型到高级别笔
│   └── signal.py                 # 一买状态机
│
├── engine/                       # 批量、增量、重构和事件编排
│   ├── historical.py             # 历史后验分类
│   ├── realtime.py               # 实时预警
│   ├── rebuild.py                # 未确认结构重构
│   └── events.py                 # 不可变结构事件
│
├── adapters/                     # 外部数据适配
│   └── binance_futures.py        # Binance BTCUSDT USDT-M K线
│
├── storage/                      # 当前状态、事件、信号、原始数据
│   ├── models.py
│   └── repository.py
│
└── application/                  # CLI/API/回放等用例编排
    ├── replay.py
    ├── inspect.py
    └── export.py
```

首版暂不放入核心：

```text
自动下单
多交易所
线段
复杂策略组合
AI分析
前端图表组件
```

## 7. 关键架构原则

1. `domain/` 不导入 pandas、ccxt、FastAPI、数据库驱动和绘图库。
2. Binance 只负责获取和标准化 K 线，不参与缠论结构判断。
3. 结构引擎输出领域对象和事件，不直接画图、不发通知、不下单。
4. 历史模式和实时模式共享同一套领域算法，区别只在输入边界和事件策略。
5. 低级别走势类型递归是 CPT 独立模块，不复用 `chanlun-pro` 的普通周期展示转换。
6. 结构对象使用不可变来源标识和 `revision`，高层结构引用低层结构 ID，不直接复制数据。
7. 先做离线回放和 JSON 导出，再接实时订阅和 Web 展示。

## 8. 当前架构决策

| 决策 | 结论 |
|---|---|
| 核心算法 | 参考 `chanlun-pro` 的基础口径，但 CPT 独立实现可审计核心 |
| 线段 | 首版不实现 |
| 基础构件 | 走势类型 |
| 多级别 | 走势类型映射为高级别结构元素，再生成新笔 |
| 数据源 | Binance BTCUSDT USDT-M 永续 |
| 运行模式 | 历史后验 + 实时预警 |
| 交易 | 首版不自动下单 |
| 存储 | 当前状态 + 不可变事件 + 信号 |
| UI | 后置，先保证核心、回放和 JSON 输出 |
