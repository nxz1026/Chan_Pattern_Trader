# deploy/ 层复盘（R45 第三轮，2026-10-04）

前两轮以**代码层**为单位（找 bug / 查文档漂移）。本轮扫 `deploy/` ——
理由很简单：它是**唯一还没系统看过、且直接决定线上跑什么**的区域。
配置漂移不会让任何测试变红，但会让线上跑着和仓库不一样的代码。

## 判据：仓内配置 vs Oracle 实际状态

**没有用「文档说的」当判据** —— 一律以 Oracle 上的实际状态为准
（`systemctl` / `crontab -l` / `diff` / `information_schema`）。

---

## 1. systemd：✅ 完全对齐

| unit | 仓内 | Oracle | 状态 |
|---|---|---|---|
| `cpt-dashboard.service` | ✅ | 已安装 | `enabled` / **`active`** |
| `cpt-dashboard-ashare.service` | ✅ | 已安装 | `static` / inactive（由 timer 拉起，正常） |
| `cpt-dashboard-ashare.timer` | ✅ | 已安装 | `enabled` / **`active`**，下次 08:00 UTC |

**没有多余 unit，也没有缺失 unit。** 3 对 3。

## 2. cron：⚠️ 三个都装了，但**一个从没被 cron 触发过**

crontab 里三条（UTC）：

```cron
20 2 * * * /home/ubuntu/bin/factor-recompute-daily.sh
40 3 * * * /home/ubuntu/bin/run-inspection-daily.sh
10 4 * * * /home/ubuntu/bin/run-metric-prune-daily.sh
```

| 脚本 | 与仓内 diff | cron 触发过几次 |
|---|---|---|
| `factor-recompute-daily.sh` | ✅ 一致 | ✅ **是**（02:20 UTC 刚跑过） |
| `run-inspection-daily.sh` | ✅ 一致 | ✅ **是**（10-03 03:40） |
| `run-metric-prune-daily.sh` | ✅ 一致 | ❌ **0 次** |

### `run-metric-prune` 的问题

日志 `/home/ubuntu/logs/run-metric-prune.log` 里**只有一条**记录：

```
===== 2026-10-03T12:53:13Z 开始（保留 run 行 90 天）=====
```

`12:53` 不是 cron 的 `04:10` ⇒ **那是 R45 装完之后手动跑的一次验证**。
⇒ **这个 cron 从装上到现在，一次都没被调度器触发过。**

**为什么这件事值得单列**：手动跑通只证明**脚本本身**能跑，
不证明 **cron 接线**能跑（PATH、环境变量加载、锁文件、重定向目标）。
「看起来排上了」和「真的跑过」是两件事，日志时间戳是唯一的判据。

**静态接线已逐项核对**（等第一次真实触发做最终确认）：

- 权限 `755` ✅ 三个脚本都是
- shebang `#!/usr/bin/env bash` ✅
- 脚本内引用的绝对路径全部存在 ✅
- 日志目录 `/home/ubuntu/logs` 存在 ✅（否则 cron 会静默丢日志）
- `cron` 服务 `active` ✅

> 第一次真实触发：**2026-10-04 04:10 UTC**。
> 已挂回执，触发后回来把结论补到本节。

## 3. nginx：✅ 文档诚实，且与线上一致

`deploy/README.md:26` 明确写着：

> ⚠️ **线上并没有安装 `deploy/nginx/cpt-dashboard.conf`。** 它是一个**独立主机**参考
> 模板；线上是把其中那几个 `location` 并进了既有的 `dsh-web` vhost。

实测确认**就是这样**：`/etc/nginx/sites-enabled/dsh-web` 里有
`location = /cpt` / `location /cpt/api/` / `location = /cpt/`，
`proxy_pass http://127.0.0.1:8010/api/` 与模板一致。

**这是本轮唯一一处「文档比实际强」的 nginx 描述 —— 而且是往安全的方向强**
（明确警告别 `cp` 到 sites-enabled，会和 dsh-web 的 `listen 80` 冲突）。
写得对，不用改。

## 4. 前端部署：⚠️ 内容全对，但**多一个公开的备份文件**

`/var/www/cpt-dashboard/` 与仓内 `dashboard/` 逐文件 diff：
**10 个文件全部一致**（含今天新增的 `url_safety.js`，线上 01:03 已部署）。

但静态根里多了一个：

```
dashboard.js.bak-r44    212 kB    2026-10-03 00:00
```

| 检查 | 结果 |
|---|---|
| 被 `index.html` 引用吗 | **0 处** ⇒ 不会被加载 |
| 含带凭据的 URL 吗 | **无** ⇒ 无凭据泄露 |
| nginx 会提供它吗 | **会**（`location /cpt/` 直接吐静态根） |

**风险低**（不加载、无凭据），但它是一个**仓库里不存在、只在线上存在**的文件，
且内容是**今天修复前的旧代码**。建议清掉；
留着的好处只是「万一要回滚」，而回滚本身用 git 更可靠。

## 5. env 模板：❌ **漏了一个键，照模版部署会静默失去所有告警**

`deploy/env/cpt-dashboard.env.example` 原本 15 个键，生产实际 17 个。
缺的是：

```
CPT_FEISHU_WEBHOOK
```

**它为什么关键**：

- `cpt/adapters/feishu.py:45` 从这个变量读 webhook；
- `deploy/cron/run-inspection-daily.sh:16` 有一行**显式警告**：
  「必须加载 env —— 否则 CPT_FEISHU_WEBHOOK 不在环境里，每次巡检都收不到告警」；
- 巡检照跑、日志照写，**只是没人在手机上收到东西** ——
  **一个不会报错的故障**，比会报错的更难发现。

`deploy/README.md:289` 有记这个变量，但**模版里没有** ——
读 README 的人知道，只看模版的人不知道。已补。

---

## 本轮改动

| 文件 | 改动 |
|---|---|
| `deploy/env/cpt-dashboard.env.example` | 补 `CPT_FEISHU_WEBHOOK` + 说明为什么不能漏 |
| `docs/review-deploy-r45.md` | 本文件（新增） |

**未改动**（核实后确认无问题）：3 个 systemd unit、3 个 cron 脚本、
nginx 模板、前端产物 —— 仓内与线上逐项一致。

## 6. 结构性缺口已修：crontab 直接指向仓内（owner 选 A）

**问题**：crontab 指向 `/home/ubuntu/bin/` 的**副本**，不是仓内文件。
两份内容当时一致，但**没有任何机制保证它们继续一致** ——
改了仓内脚本忘了 scp，线上就跑旧版，**且没有任何检查会报警**。
「两份文件靠人记得同步」本身就是问题的根源。

**方案 A（owner 选定）**：crontab 直接跑仓内路径。

### 实施前验掉的那个风险

A 的前提是「仓内路径不会被误删」。**不是靠推测，是查的**：

| 检查 | 结果 |
|---|---|
| 三个脚本是否 git 跟踪 | ✅ 全是（`git clean` 只删**未跟踪**文件，删不掉它们） |
| 脚本是否 CWD 无关 | ✅ 都自带 `cd "$REPO" || exit 1`，不依赖 cron 给的 `$HOME` |
| 解释器 | ✅ 走仓内 `.venv/bin/python`，与手动跑一致 |

`known-traps` #17 记的那个 `git clean -x` 风险**与此无关** ——
它针对的是 `.gitignore` 挡住的 `deploy/env/cpt-dashboard.env`，
不是跟踪文件。

### ⚠️ 实施时差点翻车：git 模式是 `100644`

| 脚本 | 改之前 |
|---|---|
| `factor-recompute-daily.sh` | `100644` ❌ |
| `run-inspection-daily.sh` | `100644` ❌ |
| `run-metric-prune-daily.sh` | `100755` ✅ |

**cron 不经过 shell，没有执行位就是 `Permission denied`。**
两个脚本当天是 `install -m 755` 复制到 `/home/ubuntu/bin/` 时才拿到执行位的，
**git 索引里一直没记** —— 一旦 crontab 指向仓内，立刻就会炸。
`chmod +x` 后三个都是 `100755`。

> 这是「A 方案比看起来更值得验证」的一个例子：
> 原方案（A 之前）**恰好**被 `install -m 755` 掩盖了这个缺失。
> 换成直连仓内，隐藏的假设才暴露出来。

### 端到端验证（不是「看着对」）

按 cron 的**真实调用方式**跑了一遍：

```bash
env -i HOME=/home/ubuntu PATH=/usr/bin:/bin   /home/ubuntu/DSH/Chan_Pattern_Trader/deploy/cron/run-metric-prune-daily.sh
```

`env -i` 清空全部环境、CWD 是 `$HOME` —— 与 cron 一致。
结果 **rc=0，日志 1 → 2 条**，新记录 `2026-10-04T02:40:12Z`。

**从 crontab 换了 3 行，diff 与备份逐行核对，除这 3 行外无任何改动。**
（切换前先 `crontab -l > /tmp/crontab.bak` 备份。）

### 顺带修好一处坏掉的文档

`run-metric-prune-daily.sh` 的安装说明**本身是坏的** ——
`crontab` 那行重复了两次、括号和反引号都没闭合（另两个脚本是完整的）。
照着抄会直接失败。三个脚本的安装段已重写，顺带写清了 A 的**代价**：

> 仓被 `git checkout` 到旧提交时跑的就是旧脚本 ——
> 但这本来就是 git 该有的行为，而且**改代码却不同步线上**本来就不该发生。
> 相比「默默跑一个过期副本」，这个代价小得多。

## 还没做的

- **`run-metric-prune` 的第一次真实 cron 触发**（等 04:10 UTC 后的回执；
  端到端接线已验证，只差「调度器真的会来」这一下）
- **清掉 `/var/www/cpt-dashboard/dashboard.js.bak-r44`**（低优先，
  需要用户确认 —— 线上文件删除我一般不自己动手）
