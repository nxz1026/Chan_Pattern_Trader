# dashboard/vendor — 内联第三方前端资产（R16-5）

**为什么要 vendor 进仓库**：总计划决策 **E2** 明确「lightweight-charts 内联
vendor 进仓库，不走 CDN」；R16 验收也要求**断网可用（无 CDN 请求）**。
本机是海外 IP，CDN 可达性不稳定，且看板要能在离线环境打开。

## 清单（含 sha256，便于复现与校验）

| 文件 | 版本 | 体积 | 许可证 | sha256 |
|---|---|---|---|---|
| `lightweight-charts.standalone.production.js` | 4.2.0 | 163,551 B | Apache-2.0 | `46fc69534ec098f095bbcd1d9a26d693d39a8b9eeff7343536765b3dd28c2bdf` |
| `plotly-finance.min.js` | 2.35.2 | 1,166,179 B | MIT | `55655d938260f1d0ffcc92e1aa5347a2e7fead4559eac23e76e08fcdbe495a51` |
| `bootstrap.min.css` | 5.3.0 | 232,914 B | MIT | `7f1d37f0d90b6385354c2ac10e2bb91563c46bd7a266ed351222ebcac8496c2a` |
| `bootstrap.bundle.min.js` | 5.3.0 | 80,421 B | MIT | `aa53d582f97eb594c2a5cc5824574707f9ba9837bce3046bfa5f3556860f4e04` |
| `bootstrap-icons.css` | 1.11.0 | 98,255 B | MIT | `b9e2ee3ee86f447aebb15c14fe952200ce9afcde0e6b8b693bdc0907ea444b42` |
| `fonts/bootstrap-icons.woff2` | 1.11.0 | 130,764 B | MIT | `ae167342f8ad5aad834e774ddc99528b72ac9171a684f23ed79d83ea176ca04e` |

合计 ≈ **1.87 MB**。全部来自 jsDelivr 的 npm 镜像：

```
https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js
https://cdn.jsdelivr.net/npm/plotly.js-finance-dist-min@2.35.2/plotly-finance.min.js
https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css
https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js
https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css
https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/fonts/bootstrap-icons.woff2
```

## 为什么用 `plotly-finance` 而不是完整 `plotly.min.js`

- 完整包 **4,815,814 B**（约 4.6 MB），只为了画 K 线 + 折线 + 矩形，浪费 4 倍体积；
- `plotly.js-finance-dist-min` **1,166,179 B**，已验证包含 `candlestick` / `ohlc`
  / `scatter` / `shapes`（`grep -c candlestick plotly-finance.min.js` → 1，
  压缩后仍保留 trace 名），本画布 C/D 需要的图元全都有。

## 为什么 bootstrap 只在画布 D 的 iframe 里加载

`bootstrap.min.css` 会重排全局样式（`.container` / `.row` / `.table` …），直接放进
主页面会打乱现有 CPT 看板（R12 刚验过 375px 移动端触摸目标与水平溢出）。
画布 D 因此渲染在**同源 iframe** 里，bootstrap 只注入那个 iframe 的 document，
主页面零污染。

## 升级方式

改版本号 → 重新下载 → 更新本文件的 sha256 → 跑
`node /home/ubuntu/work/cpt-audit/audit_R16.js`（含「无 CDN 请求」断言）。
