# dashboard/ 层复盘（R45 第四轮，2026-10-04，无头浏览器）

前三轮都以**源码/配置/文档**为单位。第四轮换成**跑起来看** ——
无头 Chromium 登录线上看板，逐个画布、逐个市场实测。

## 为什么源码审不出来

前几轮反复撞到同一件事：**代码「看起来是好的」不等于它在浏览器里工作**。
这一轮抓到 5 个问题，全部是源码层面看不出问题的：

| # | 问题 | 源码能看出来吗 | 用户能感觉到吗 |
|---|---|---|---|
| 1 | `q is not defined`（画布 D 诊断） | 能，但很容易漏 | **完全看不到**（错误信息被自己吞掉） |
| 2 | `bootstrap-icons` 永远加载失败 | 不能 | 只污染 console，看不见 |
| 3 | 差异点**全是黑的** | **不能** | **看不出来**（但功能「像是能用」） |
| 4 | `mismatched` 态没配色 | 不能 | 看不出来 |
| 5 | 差异点第 4 行被裁一半 | **不能** | 看不出来 |

**#3 是本轮最重要的一个**：一个「能点、能交互、看起来是好的」面板，
但它传达**零信息量** —— 60 个点和 0 个差异看起来一模一样。

## 判据

不靠「页面能打开」，而是四类可测量的东西：

1. console 报错 / 未捕获异常（`pageerror` + `console` 双通道）
2. 请求失败与 HTTP >= 400
3. 页面可见文案里的 `undefined` / `NaN` / `[object Object]` / 占位符残留
4. **渲染结果本身**：SVG 图元数、canvas 非透明像素占比、计算样式

以及**截图实看** —— #5 就是数字全绿、只有看图才发现的。

---

## 1. `q is not defined` —— 画布 D 的诊断显示彻底失效

`dashboard/canvas_d.js:212` 调 `q("[data-testid=canvas-d-diag]")`，
而**本文件从头到尾没有定义过 `q`**。

**后果不是「诊断偶尔不显示」，是「诊断永远不显示」**：
上一行 `node.dataset.canvasDiag` 确实写进去了，但这一行就抛，
下面的 `createElement` / `appendChild` / `textContent` 永远到不了。

而这正是「wbt 的 vendor 脚本加载失败」时**唯一**会把原因显示到屏幕上的路径 ——
**报错信息被自己的 ReferenceError 吞掉了**。

顺带修了作用域：框是 append 到 `node` 上的，原来却用全文档范围查找 ⇒
页面上有多个画布节点时第一个的框会被第二个抢走。

## 2. `bootstrap-icons` 加载不成功，而且根本没人用

每渲染一次画布 D 产生 **4 条 console 错误**（2 条 CORS + 2 条 `ERR_FAILED`）：

```
Access to font at '.../bootstrap-icons.woff2' from origin 'null'
  has been blocked by CORS policy
```

**根因是安全设计，不是配置错误**：iframe 是 `sandbox="allow-scripts"`，
按 **R28-11** 的决定**刻意不带** `allow-same-origin`
（旧组合有已知逃逸：帧内脚本能 `frameElement.removeAttribute("sandbox")`
再重载从而拿到父页 origin）。所以 srcdoc 文档 origin 是不透明源 `"null"`，
而**字体是 CORS 受限资源**。

⇒ **绝对不能把 `allow-same-origin` 加回去**。

**而且就算能加载也没人用**：对 `references/wbt/` 全量 grep，
`bi-*` 图标类出现数为 **0**。原注释写的「wbt 模板用了 … .bi 图标」
对锁定的 wbt 0.9.1 **不成立**。

留着它是有害的：它**看起来**像「图标能用」，实际永远不能。已删除该 `<link>`。

> 若将来 wbt 真要用图标字体，正确做法是把字体**内联成 `data:` URI**
> —— data: URI 不受 CORS 约束，且不破坏 sandbox 边界。

## 3 + 4. 差异点配色：三条规则从来不存在，而且是**四态不是三态**

`dashboard.js` 给每个结构元素画一个可点击圆点，
class 是 `parity-${status}`，而 `cpt/application/dashboard_parity.py:44` 写明：

```
status ∈ matched | missing | extra | mismatched
```

**这三条（乃至四条）CSS 规则一直不存在** —— 只有 `[data-parity-selected]`
有样式。于是所有圆点都用 **SVG 默认填充黑**。

⇒ 匹配 / 缺失 / 多余**长得完全一样**。这个面板的全部意义就是
一眼看出 CPT 和 ORACLE 哪儿对不上，而现在传达不了任何信息。
**交互没坏（还能点），所以它看起来是好的** —— 这比直接报错更难发现。

修的时候我又犯了一次同样的错：

> 我 grep 到的状态值是 `matched/missing/extra` 三个，
> **那是我预期的那三个**。上色后跑实测，DOM 里冒出
> **`mismatched`: 2** —— 颜色列表里赫然多出一个 `rgb(0,0,0)`。
> 翻回 `dashboard_parity.py:44` 才发现是**四态**，
> 而 `mismatched`（两边都有但字段对不上）恰恰是**最值得看的一类**。

四态现在用既有语义色变量区分（不引入新色）：

| 状态 | 颜色 | 含义 |
|---|---|---|
| `matched` | `--color-text-muted` | 一致 |
| `mismatched` | `--color-accent` | 有出入（**最该看的**） |
| `missing` | `--color-error` | ORACLE 缺 |
| `extra` | `--color-degraded` | ORACLE 多 |

并补了**带真实计数的图例** —— 光有颜色是歧义的，观者无从知道红点
代表「缺」还是「多」；数字比颜色更有用。

## 5. viewBox 高度写死，超 72 个元素就裁图

`viewBox` 原写死 `0 0 640 150`，而布局是 24 列 × 35 行距 + 半径 7：
**超过 72 个元素（3 行）时第 4 行 `cy=150`，圆心+半径 157 ⇒ 越界 7px 被裁**。

实测 Oracle 上 CPT 侧 84 个点 ⇒ **第 4 行正好被切一半**。
这个只有看截图才发现 —— 元素计数、console、请求全是绿的。

已改成按实际行数算高度，并把 `r: 7` 提为常量 `R`（否则两处会漂移）。

---

## 不是问题的（避免误记）

- **「暂无」文案**：全部是**合法空态** —— `暂无信号` 对应当前标的/时间窗
  确实没有信号；`暂无数据` 那个 H2 本身 `offsetParent === null`（隐藏模板）。
- **画布 D 主区空白**：面板下方两行写明「画布 D 的成交量不在 wbt 报告里
  （见画布 A）」「画布 D 不渲染 MACD 副图（见画布 A）」—— 是**设计说明**，不是故障。
- **canvas B 的 `0.0%` 像素 canvas**：`lightweight-charts` 的主/副图配对，
  副图容器本身不画，是正常的。

## 改动

| 文件 | 改动 |
|---|---|
| `dashboard/canvas_d.js` | 修 `q` 未定义；删无用的 `bootstrap-icons` link |
| `dashboard/dashboard.js` | 差异点 viewBox 按行数算；`r` 提常量；补四态图例 + 计数 |
| `dashboard/dashboard.css` | 补四态配色 + 图例样式 + focus 可见性 |

## 验证

- console 错误/未捕获异常：**6 → 0**（同一套无头判据，改前改后各跑一次）
- 四态颜色全部生效，计算样式里**不再有 `rgb(0,0,0)`**
- 回归：Oracle 全量 `pytest tests/` = **2 个失败，与基线逐条相同**
  （`test_dashboard_runs_index.py::test_timestamp_falls_back_when_runtime_omits_generated_at`、
  `test_dashboard_wiring_d.py::test_signal_stats_route_degrades_when_history_unavailable`）

### 一条插曲：我自己踩了守卫

改完跑测试，**多出一个失败**
`test_dashboard_ashare_contract.py::test_canvas_d_only_forwards_market_context`
（`assert 2 == 1`，`a_share` 出现 2 次）。

原因是我在注释里写了 `market_a_share.js`（另一个文件名），
**子串 `a_share` 撞上了这个字符串计数守卫**。

⇒ 改的是**我的注释措辞**，不是守卫 —— 守卫没坏，
是我的注释脏。这正是 `known-traps` #4 说的那类字符串断言的固有粗糙性，
但这次它抓到的是真问题（我的），所以不该放宽它。

## 还没做的

- `deploy/dashboard-sync.sh:33` 硬编码了默认 Basic Auth 口令
  （`admin:ndjack`）—— 可被 `CPT_BASIC_AUTH` 覆盖，但默认值躺在仓里。
  owner 已明确表示不轮换口令，此处**只记录**。
- `/var/www/cpt-dashboard/dashboard.js.bak-r44` 仍在线上（等 owner 点头删）。

---

## 追加：画布 D 主区**始终空白**（已定位到边界，未修）

上一轮我把它列在「不是问题的」里（以为是等 plotly 加载的时间不够）。
**那个判断是错的** —— 等到 12 秒仍然空白。修正如下。

### 排除掉的东西（都实测过）

| 怀疑 | 实测 | 结论 |
|---|---|---|
| wbt 没装 | `import wbt.report` 成功 | 排除 |
| payload 造不出来 | `available: True`，`source: wbt.report.HtmlReportBuilder@0.9.1` | 排除 |
| K 线数据是空 | `counts: {candles: 122, fractals: 39, bis: 38, zhongshus: 7}` | 排除 |
| **plotly `x` 轴全是 null** | 真实快照下 **122 项全非 null**，日期 `2026-04-07…` 起 | 排除 |
| vendor 加载失败 | 诊断框报 `OK plotly \| OK bootstrap.bundle`；直连 `/cpt/vendor/*` 三个文件均 200 | 排除 |
| 图表 div 不存在 / 高度为 0 | `<div style="height:520px">` + 内部 `height:100%`，外层 `tab-pane fade show active` | 排除 |

> ⚠️ 中间踩了一次自己的坑：我第一版测试快照把日期字段写成 `dt`，
> 而真实快照是 **`open_time`**（字符串毫秒）。于是我造出了一份
> 「`x` 全是 null」的假证据，差点当成真 bug 报上去。
> ⇒ **判据必须用真实 API 响应，不能用自己拼的样本。**
> 这是今天第 N 次「我造的证据」反过来骗我。

### 已知的真正约束

`contentDocument` **跨域不可读** —— iframe 是 `sandbox="allow-scripts"`，
origin 是不透明源。这是 R28-11 的**安全设计**，不是缺陷。
⇒ 父页**无法**从外部判断 iframe 内的 plotly 到底画没画。

### 剩下的最可能原因（未验证）

`srcdoc` 执行时 iframe 可能**尚未被父页定宽**（父页在 load 之后才设尺寸），
plotly 于是按 0 宽容器绘制；而 iframe 内的补救逻辑
（`resizeActivePanes()`，跑在 `DOMContentLoaded` / `window.load`）
**同样发生在定宽之前**，于是没有任何后续动作把它重画回来。
父页切到画布 D 时也**没有**向 iframe 发 resize 消息。

### 为什么这条值得单列：诊断通道本身有缺口

`reportDiag` 只报 `vendor-ok` / `vendor-fail` ——
**它不报 `newPlot` 是否执行、是否抛异常、渲染出的容器是几乘几**。
所以修复 #1 让诊断框能用了之后，诊断框只能告诉我
「vendor 加载成功」，**恰恰说不出「图为什么没画出来」**。

⇒ 下一步该做的是往这条通道补三个信号
（`plot-ok` / `plot-throw` / `plot-size`），而不是盲改样式。
**本轮不做** —— 它改的是 iframe 通信契约，值得单独一轮。

---

## 追加：帧内探针的**失败尝试**（已回退，记录以免重踩）

**做法**：往 srcdoc 里追加一段探针脚本，包一层 `Plotly.newPlot`，
用 `postMessage` 回报 `plot-throw` / `plot-size` / `plot-blank` / `plot-miss`。
动机是对的 —— iframe 是不透明源，父页**读不到** `contentDocument`，
不新增这条通道就永远说不清「图为什么没画」。

**结果：直接把生产搞坏了。** 追加探针后：
- 页面抛 `TypeError`，`str(e)` 就是**探针脚本全文**；
- **画布 D 的标签按钮整个消失**（`A/B/C` 三个还在，`dMention: false`）。

**处置**：`git checkout -- dashboard/canvas_d.js` 回到 `ff6f775`，
重新部署，D 按钮恢复，console 归零。**生产当前是好的。**

### 两件必须说清楚的事

**1. 我没有定位到 TypeError 的确切原因 —— 不猜。**
已排除：探针 JS 语法正确（`node --check` 过）、
无提前闭合的 `</script`、无 `${}` 被父页模板提前求值、
`url_safety.js` 不抛、message 监听只有一处。
`name='TypeError'` 而 message 是整段脚本源码，这个组合我没见过，
**没有可靠解释就不写成结论** —— 今天已经因为「拿猜的原因当结论」栽过两次。

**2. 第一版探针的信号本身是错的（这个查清了）。**
它数 `.nsewdrag` 当「图元数」并报「图元 220」——
而 `.nsewdrag` 是 plotly **每个**图 div 都有的 resize 手柄，
只要 `newPlot` 走到过就必然 ≥1。
⇒ 「220 个图元」**不能证明 K 线画出来了**，而实测画面确实是白的。
已改成数 `path.point-plot`（蜡烛）并报 SVG 尺寸 —— 但因上面那个 TypeError
**未能实测验证**，所以这条改动没有保留。

### 下次要做的话，方向

不要再往 `srcdoc` 里塞**第 N+1 段**内联脚本。
更稳的路子是：把探针逻辑**并进已有的片段脚本**（`inline` 那一段），
或让 `reportDiag` 的既有通道复用已存在的 vendor `onload` 回调，
而不是新增独立 `<script>`。**先在一份离线 srcdoc 上复现，再上生产。**
