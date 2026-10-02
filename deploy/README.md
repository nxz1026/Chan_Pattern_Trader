# CPT 本地 Dashboard 部署

## 手工起 API（调试用）

在仓库根目录执行：

```bash
cp deploy/env/cpt-dashboard.env.example deploy/env/cpt-dashboard.env
.venv/bin/python -m cpt.web --host 127.0.0.1 --port 8010 --mode realtime --poll-seconds 30
```

验证：

```bash
curl http://127.0.0.1:8010/api/dashboard/health
curl http://127.0.0.1:8010/api/dashboard/snapshot
```

> **端口用 8010，不要用 8000。** 8000 已被本机另一个项目（Resume-Matcher 的
> uvicorn）占用，绑定会失败；而线上 nginx 的 `/cpt/api/` 也是反代到 8010
> （见 `/etc/nginx/sites-enabled/dsh-web`）。`deploy/env/*.example` 里的默认值
> 已按线上实况填好，照抄即可。

## Nginx

⚠️ **线上并没有安装 `deploy/nginx/cpt-dashboard.conf`。** 它是一个**独立主机**参考
模板（自带 `listen 80` server 块）；线上是把其中那几个 `location` 并进了既有的
`dsh-web` vhost（监听 443 ssl），见 `/etc/nginx/sites-enabled/dsh-web`。

因此在本机这种"80/443 已被 dsh-web 占用"的环境里：

- **不要** `cp deploy/nginx/cpt-dashboard.conf /etc/nginx/sites-enabled/` ——
  会与 dsh-web 的 `listen 80` 冲突；
- 正确做法是把文件里的 `location = /cpt`、`location /cpt/api/`、`location = /cpt/`、
  `location /cpt/` 四块摘进 dsh-web，再 `nginx -t && systemctl reload nginx`。

只有在独立主机或独立 vhost 上才整块使用本文件。模板里的
`proxy_read_timeout`/301 跳转已与线上实测对齐。Nginx 仅提供静态 Dashboard 和
`/cpt/api/` 反向代理；Python API 必须先运行。

## 部署静态看板

Nginx 的静态根是 `/var/www/cpt-dashboard`（见 `deploy/nginx/cpt-dashboard.conf`）。
更新前端 = 把 `dashboard/` 下的产物拷进去：

```bash
sudo cp dashboard/{index.html,dashboard.css,dashboard.js,canvas_*.js,market_a_share.js} \
        /var/www/cpt-dashboard/
# 首次或依赖有变时还要拷 vendor/
sudo cp -r dashboard/vendor /var/www/cpt-dashboard/
```

> ⚠️ **从 Windows 部署必须先归一化换行**（2026-10-02 R34 实测踩了两次）。
> Windows 检出的 `dashboard.js` 是 **CRLF**，仓里的规范内容与服务器都是 **LF**
> （`git ls-files --eol` → `i/lf w/crlf`）。不归一化的话：
>
> - 静态根与仓永远对不上，之后每次漂移检查都在演假警报（R34 第一次就因此误判
>   「线上落后 4,761 字节」，而 4,761 正好是 CRLF 行数）；
> - 反过来，scp 上去的就是 CRLF 版，**真的**把偏离部署进了生产。
>
> ```bash
> # 在开发机上先归一化再 scp，或在服务器上：
> tr -d '\r' < dashboard.js.new > dashboard.lf.js
> sudo install -m 644 -o ubuntu -g ubuntu dashboard.lf.js /var/www/cpt-dashboard/dashboard.js
> ```
>
> `install -m 644` 而不是 `cp`：09-29 那 3 次 `run6.sh` 的 403 就是文件被给成
> 0600 造成的（`error.log`：`open() failed (13: Permission denied)`）。

**权限用 `X`（大写），不要写 `chmod 644 *`**：

```bash
sudo chown -R ubuntu:ubuntu /var/www/cpt-dashboard
sudo chmod -R u=rwX,go=rX /var/www/cpt-dashboard   # X = 只给目录加执行位
```

踩过的坑：`sudo chmod 644 /var/www/cpt-dashboard/*` 会把 **`vendor/` 目录**的
执行位也去掉（变成 `drw-r--r--`），Nginx 无法穿越该目录 →
`vendor/*.js` 全部 **403 Forbidden**。页面上表现为画布 B/C 的图表库加载失败，
而 HTML/CSS 看起来完全正常，很容易误判成"前端代码写错了"。
`u=rwX,go=rX` 对文件给 `rw-r--r--`、对目录给 `drwxr-xr-x`，一次就对。

改完后按文件名逐个 `diff -q` 确认与仓库一致（`vendor/` 也要比对），再刷新页面。

## systemd

`deploy/systemd/cpt-dashboard.service` 是 hardened 模板（`NoNewPrivileges=true`
`PrivateTmp=true` `EnvironmentFile=` + `Restart=on-failure`）。安装前**先创建环境文件**，
否则服务会拒绝启动（模板里的 `EnvironmentFile=` 故意不带 `-` 前缀，文件缺失时直接报
`Failed to load environment files`，而不是静默把参数展开成空串）：

```bash
# 环境文件（不入仓，需手动创建）
cat > deploy/env/cpt-dashboard.env << 'EOF'
CPT_HOST=127.0.0.1
CPT_PORT=8010
CPT_MODE=realtime
CPT_POLL_SECONDS=30
CPT_SYMBOL=BTCUSDT
CPT_INTERVAL=1h
EOF

sudo cp deploy/systemd/cpt-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cpt-dashboard
sudo systemctl status cpt-dashboard
```

`--mode realtime` **已可用**（真实 engine snapshot provider 已接线）：线上服务就是
`--mode realtime --poll-seconds 30`，`/api/dashboard/health` 返回
`ok=true / degraded=false`。需要空数据调试时才用 `CPT_MODE=demo`；不要把 demo 的
空 snapshot 当作真实行情。

> 2026-09-30 状态：**服务已由 systemd 托管**。安装于 2026-09-29 经宿主机 namespace
> 完成（容器内 systemd 需 interactive auth 不可用，改用
> `docker run --rm -i --privileged --pid=host -v /:/host <img> chroot /host ...` 写入单元）。
>
> 实测证据（可复现）：
>
> ```
> systemctl show cpt-dashboard -p ActiveState -p UnitFileState -p MainPID
>   → ActiveState=active  UnitFileState=enabled  MainPID=<pid>
> ps -o pid,ppid,cmd -p <pid>
>   → PPID=1（systemd），无 nohup 残留
> ```
>
> ⚠️ 历史口径：本文与 `docs/audit/cpt-audit-20260929.md` 曾写"服务以 nohup 运行、
> 不享受自动重启"，那是**安装完成前**的状态，已于 2026-09-30 更正。

## A 股快照 timer（每日写 cpt_signal_event）

主服务 `cpt-dashboard` 只跑加密行情（`--mode realtime --symbol BTCUSDT`），
不触发 `record_signal_event` → `public.cpt_signal_event` 表存在但长期为空，
`/api/dashboard/signal-stats` 一直 `total:0`。补法：**并列**起一个 oneshot service +
timer，每天 08:00 UTC（北京时间 16:00）触发一次 `scripts/snapshot_a_share_batch.py`，
对 `hot_pool Top 50 ∪ 服务端 watchlist` 循环调 `build_ashare_snapshot`，里面
自动推进状态机、status 变化时 INSERT 一条事件。

```bash
sudo cp deploy/systemd/cpt-dashboard-ashare.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cpt-dashboard-ashare.timer   # 装上 timer（每天 08:00 UTC 触发）
# 立刻手动触发一次验证（不等明天）：
sudo systemctl start cpt-dashboard-ashare.service
sudo journalctl -u cpt-dashboard-ashare -n 200 --no-pager
# 验表里进了行：
PGPASSWORD=$(grep '^\$DB_PW' ~/.dbconfig | cut -d= -f2-) \
  psql -h 127.0.0.1 -U postgres -d emotion_core \
  -c 'SELECT COUNT(*) FROM public.cpt_signal_event;'
```

设计要点：

- **oneshot**（不是 simple），跑完即退；`systemctl enable --now cpt-dashboard-ashare.timer`
  才会按 schedule 触发；不带 `--now` 也不会开机自跑（只 enable 不启动）。
- **`Persistent=true`**：上次因机器关停错过的时间点，开机后会补跑一次（避免周末
  /夜间重启导致的事件链长期空段）。
- **不引 `EnvironmentFile=cpt-dashboard.env`**：本脚本不需要那 6 个加密参数，避免
  与主服务形成隐式耦合（删那个 env 文件不应打挂 A 股快照）。
- **DB 鉴权走 `~/.dbconfig`**：`AShareLocalClient` 默认 lazy 连接读
  `pathlib.Path.home()/".dbconfig"`（仅 ubuntu 可读 600）。如果哪天 env 与 home
  文件冲突以 home 为准。
- **跟主服务错开时间**：主服务每 30s 拉加密行情；快照跑的那一两分钟 A 股库的
  SELECT/INSERT 会多一点，但 `factor_ensurer` 是 lazy + 单只范围隔离，影响有限。
  仍嫌吵把 timer 调到 `OnCalendar=*-*-* 08:30:00 UTC` 错开 30 分钟即可。

> 2026-09-30 状态：单元已写入 `deploy/systemd/cpt-dashboard-ashare.{service,timer}`
> 并随仓推送；首次安装命令见上方。

## 数据库迁移（R20 / R21 / R23）

三份迁移都在 `scripts/migrations/`，都是 `CREATE TABLE / INDEX IF NOT EXISTS`
的**幂等**脚本，可重复跑。

| 文件 | 表 | 用途 |
|---|---|---|
| `2026-09-30_r20_factor_columns.sql` | `asel.ref_adjust_factor` | 补因子列 |
| `2026-10-01_r21_signal_event.sql` | `public.cpt_signal_event` | 信号事件流（A 阶段） |
| `2026-10-02_r23_dashboard_run.sql` | `public.cpt_dashboard_run` | 运行持久化，跨重启可比（B 阶段） |

在 oracle 上安装：

```bash
# 1) 推到 /tmp
scp ./scripts/migrations/2026-10-02_r23_dashboard_run.sql oracle:/tmp/

# 2) 跑（幂等，可重跑）
ssh oracle 'PGPASSWORD=$(grep "^\$DB_PW" ~/.dbconfig | cut -d= -f2-) \
  psql -h 127.0.0.1 -U postgres -d emotion_core -v ON_ERROR_STOP=1 \
  -f /tmp/2026-10-02_r23_dashboard_run.sql'

# 3) 验表建好（应为 5 列 + 2 索引）
ssh oracle 'PGPASSWORD=$(grep "^\$DB_PW" ~/.dbconfig | cut -d= -f2-) \
  psql -h 127.0.0.1 -U postgres -d emotion_core -At -c \
  "SELECT column_name FROM information_schema.columns \
   WHERE table_name='"'"'cpt_dashboard_run'"'"' ORDER BY ordinal_position;"'
```

⚠️ **dbname 不是默认的 `longkonglong`**：`cpt/adapters/_dbconfig.py` 里的
`dbname="longkonglong"` 只是 fallback，权威是 `~/.dbconfig` 的 `$DBNAME`
（线上 = `emotion_core`）。启动前先 `cat ~/.dbconfig | grep DBNAME` 确认。

⚠️ **psql 必须显式 `-h 127.0.0.1 -U postgres`**：peer auth 不接 `ubuntu`
用户，直连会报 `FATAL: role "ubuntu" does not exist`。

### R23 装完必须重启主服务

`cpt-dashboard` 是 Python 进程，**启动时一次性 import**，改 `cpt/web/app.py`
加新路由**必须**重启进程才生效——CI 通过 ≠ oracle 上生效：

```bash
ssh oracle 'sudo -n systemctl restart cpt-dashboard && sleep 3 && \
  sudo -n journalctl -u cpt-dashboard -n 20 --no-pager'
# 期望日志出现：CPT Dashboard API listening on http://127.0.0.1:8010 (mode=realtime)
```

装完 R23 后 `/api/dashboard/runs`、`/compare`、`/multi-run` 会**优先读表**；
表不可用时自动回落到进程内环形缓冲（退化成 R20 行为，不会 500）。

表是 **append-only 且不自动 GC**。realtime 30s 一轮 ≈ 2,880 行/天、jsonb
平均 30KB，1 月约 2.5 GB（PG vacuum 后稳定）。运维想清理就手动：

```sql
DELETE FROM public.cpt_dashboard_run
  WHERE generated_at < now() - interval '7 days';
```

> 本仓刻意不在 HTTP 请求路径上跑大 SQL 做自动 GC。
> 回滚就是 `DROP TABLE IF EXISTS public.cpt_dashboard_run CASCADE;`——表无 FK，
> 代价是 0。**不要 DROP `public.cpt_signal_event`**，那是 R21 的真数据。

## `/cpt/` 的 Basic Auth（R30 补上）与一个操作坑

2026-10-02 补：`/cpt/` 此前是同一个 nginx 站点上**唯一没有 `auth_basic` 的项目**
（`/emotion/` `/dashboard/` `/resume` 三个都有），静态看板与 `/cpt/api/` 全部匿名
可读可写地挂在公网上。现在三处都挂了 `auth_basic "Restricted"` + `/etc/nginx/.htpasswd`。

### 坑一：三个 location 都要加，漏一个等于没做

    location /cpt/api/     ← 独立块，且比 /cpt/ 更具体，nginx 按最长前缀匹配
    location = /cpt/       ← 静态首页
    location /cpt/         ← 静态资源

只给静态两块加认证时，实测是 `/cpt/` → 401 而 `/cpt/api/...` → **200 无凭据可读
可写**。看起来做了、其实没做 —— 比完全没做更危险。

### 坑二：不能用 URL 内嵌凭据驱动这个页面

`https://admin:xxx@host/cpt/` 这种写法会让页面里的相对 `fetch` **继承凭据**，
浏览器直接抛：

    Failed to execute 'fetch' on 'Window': Request cannot be constructed from a
    URL that includes credentials

也就是说**浏览器自动化验这个看板不能靠 URL 带账密**。可行的做法是让一个本地
代理在服务端补 `Authorization: Basic ...` 头，浏览器侧完全看不到凭据。
（headless Chrome 的 `--ignore-certificate-errors` 能过自签证书那关，过不了这关。）

### 内部直连不受影响

部署与健康检查脚本走 `http://127.0.0.1:8010/...`（不经 nginx），所以加认证
**不会**影响自动化验证 —— 只需注意别改成走公网 URL。

## `_pkg/` — ⚠️ 混进来的另一个项目，不属于 CPT

`/var/www/cpt-dashboard/_pkg/` 里放的是 **`collector-cn` 采集机**的发布产物
（`collector-cn-*.tgz` + `run5.sh`），**不是 CPT 的东西**。`run5.sh` 的用途是在
远端采集机上执行：

```bash
curl -sSk https://<host>/cpt/_pkg/run5.sh | bash
```

即「一条命令装采集机」。所以这个目录是**故意的发布通道**，不是垃圾 —— 但它有三个
问题，2026-10-01 记录在此：

1. **不在版本控制下**。这些 `.tgz` 只存在于那台机器，仓里没有、CPT 的
   `deploy/` 也没有对应脚本或说明。
2. **已积压 8 个版本**（`collector-cn-dd2b9b8.tgz` + `linux1` ~ `linux7`），
   合计 516 KB，**没有任何清理机制**。
3. **CPT 的部署文档此前对它只字未提**，导致每次看 `/var/www/cpt-dashboard/`
   都会以为「线上有仓里没有的文件」。

### 现状实测（2026-10-02）—— 谁在用、谁没人用

逐个核对了脚本里引用的包名与期望 sha256：

| 脚本 | 引用的包 | sha256 是否对得上 |
|---|---|---|
| `run5.sh` | `collector-cn-dd2b9b8-linux5.tgz` | ✅ `66e2bd84…` 一致 |
| `run6.sh` | `collector-cn-dd2b9b8-linux6.tgz` | ✅ `ff297dfa…` 一致 |

**两个脚本都是活的**（各自的包与 sha 都对得上）。所以「只保留 run5.sh 里那一个」
这条老建议是**错的** —— 按它执行会把 `run6.sh` 的包删掉，直接打断一条正在用的
一键安装链路。

**无人引用的 6 个**：`collector-cn-dd2b9b8.tgz`（base）、`linux1` ~ `linux4`、
以及 `linux7`。合计约 380 KB。

### `linux7` 归属：**上一版这里写错了**（2026-10-02 R31 错 → R33 更正）

> ⚠️ **R31 在这一节写过「这条安装通道从来没被用过，`_pkg` 命中 0」——那是错的。**
> 错因：我只查了 `/var/log/nginx/access.log`，而 `/cpt/` 这个 location 在
> `dsh-web:31` 上写的是 `access_log /var/log/nginx/dsh-timing.log dsh_timing`
> —— **access.log 根本不记这个 location**。`dsh-timing.log`（含轮转）里
> `_pkg` / `.tgz` / `run*.sh` 合计 **64 条命中**。
>
> **教训：查「有没有人用过」之前，先确认请求会记在哪个日志里。** 一个存在但
> 记错位置的日志，比没有日志更危险 —— 它给你一个假的 0。

**更正后的事实（逐条可复现）：**

| 结论 | 证据 |
|---|---|
| 这条通道**在用** | `dsh-timing.log` 三个文件合计 32 条 `_pkg` 命中；`error.log.3.gz` 另有 3 条 |
| **两个来源 IP** | `110.40.203.130`（**外部采集机**，14 条）、`140.83.62.161`（oracle 自身公网 IP，16 条）、`127.0.0.1`（2 条） |
| **整条版本史都被下过** | base ×3、linux1 ×1、linux2 ×1、linux3 ×2、linux4 ×2、linux5 ×4、linux6 ×5、**linux7 ×2** |
| **linux7 上传后 2 分钟就被取走** | 上传 `09-29 08:32:43`（oracle 公网 IP）→ 下载 `09-29 08:34:07`（`110.40.203.130`，ua=curl/8.5.0） |
| 有人在重试一键安装 | `09-29 07:37:19` 连 3 次 `GET /cpt/_pkg/run6.sh` → **403 Permission denied**（文件当时是 0600），`07:37:50` 才拿到 200 |

**所以 `linux7` 不是孤儿**：外部机器按直链 URL 取了它，缺的只是一个 `run7.sh`
接线（见下）。R31 说它「归属存疑」也是错的。

#### 但 R31 有一条判断**成立**，而且比原话更严重

R31 说「不要照抄 `run6.sh` 做 `run7.sh`，照抄必挂」—— 对，但那只是症状。**根因是
`linux7` 这个包本身是坏的**：

    linux7 的 scripts/scheduler.py:35   RUNNER = HERE / "collector_linux.sh"
    而 linux7 的包里没有这个文件（linux6 有；两个包的 scheduler.py 逐字节相同）

后果**不是崩溃**：`scheduler.py` 的 `run()` 捕获 `OSError` → 记
「mode=X 无法启动」`rc=-2` → `supervise.sh` 每 5s 重启一次。于是
**进程活着、日志有输出、采集量恒为 0** —— 绿色的坏掉。

（另注：`linux7` 补进 `collector.py` 的 `os.umask(0o022)` 是**加固**而非抢修 ——
`collector_linux.sh` 开头本来就有 `umask 022`；而 `incoming/cn-collector/` 下
6,573 个文件里 0600 只有 1 个，还是 `league` 自己的探测文件。）

#### 处置（R33，owner 拍板「补一个真正的 run7.sh」）

已发布 `_pkg/run7.sh`，它与 `run5`/`run6` 的唯一实质差别在第 4b 步：
**补回 linux7 打包时掉的那个 runner** —— 从 linux6 包取回
`scripts/collector_linux.sh`（实测与 linux6 那份**逐字节相同**），装完在第 4c 步
**断言**它存在且可执行，补不回来就 `exit 1`，绝不留下一个装得上、采不到数的
采集机。发布前做了**截断演练**（真下载 + 真校验 + 真解包 + 真补回，`WORK` 指向临时
目录，只走到 4c），全绿。

> **给 collector-cn 主人的真问题**：应该出一个重新打包的版本（把
> `collector_linux.sh` 放回去），而不是靠 run 脚本补。run7.sh 是止血，
> 不是修包。

### ⚠️ 三个 run 脚本的用法注释都曾「照抄跑不通」（R33 一并更正）

`/cpt/` 挂 `auth_basic`（R30 加的，09-30 之后），而三个脚本是 **09-29** 写的 ——
所以它们注释里那行 `curl -sSk <url> | bash` **不带凭据**，实测返回
**172 字节的 401 HTML**，等于把 401 页面喂给 bash：

    无凭据: http=401 bytes=172
    带凭据: http=200 bytes=55319（sha 与磁盘一致）

已在 `run5.sh` / `run6.sh` / `run7.sh` 里统一改成从环境变量 `CPT_AUTH` 读凭据
（**不写进文件**，避免它变成秘密载体），并在注释里写明为什么。原文件已按时间戳
备份（`run5.sh.bak.*` / `run6.sh.bak.*`）。改完逐个从 nginx 取回、`bash -n`、
与磁盘逐字节比对，三项全过。

> ⚠️ `linux7` 包**内部**那份 `scripts/run5.sh`（指向 linux5 包的陈旧残留）也还是
> 旧用法 —— 那在别人的 tgz 里，重打包时一并清掉。

### 清理规矩（仍然有效，但注意第 2 条已不止是「理论上」）

1. **只删「没有任何 run*.sh 引用」的 tgz**，且删前逐个确认 sha256 没被引用；
2. **保留 `run*.sh` 引用的每一个包** —— 删掉就是打断那条一键安装链路。
   ⚠️ R33 已证实这不是理论风险：`linux1`~`linux7` 全都被外部机器取过；
3. 删之前先 `cp` 到 `/tmp` 留一份（可恢复），再从 `_pkg/` 移走；
4. 更根本的做法：给 `collector-cn` 单独一个 nginx `location` 或独立静态根，
   别把另一个项目的发布通道寄居在 CPT 的看板目录里。

> **本轮没有执行任何删除。** 理由：这是另一个项目的产物、位于生产、且删除不可逆；
> 而 `linux7` 的归属本身就是个未解的问题。规矩先写在这里，等 owner 确认。

> 检查线上是否漂移：
> `md5sum dashboard/* 与 sudo md5sum /var/www/cpt-dashboard/*` 逐个比对。
> 2026-10-01 实测 8 个文件全部一致。

## 安全边界

- **行情与结构数据只读** —— 不提供下单、撤单、账户、持仓或订单簿接口。
- ⚠️ **但自选列表可写**：`POST/DELETE /api/dashboard/a-share/watchlist`。
  处置是要求 `Content-Type: application/json`（否则 415）以抬高跨站触发门槛，
  **不引入鉴权系统**（`app.py:888` 有注释说明这是刻意取舍）。所以「只读」这句话
  只对行情/结构成立，别对外笼统承诺。
- **不要把生产库口令写进任何版本化文档**。2026-09-30 的交接文档曾明文写出
  `$DB_PW`，2026-10-01 已删除，但**口令已进 git 历史，需要轮换**
  （待办见该交接文档 §3.3）。
- 不把交易所密钥放入前端、Nginx 配置或 Git。
- 本地上线前确认 Nginx 不暴露 `.git`、`.venv`、`references`。
  （`_pkg/` 是有意暴露的发布通道，见上一节。）
