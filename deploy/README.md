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

## 安全边界

- API 只读，不提供下单、撤单、账户、持仓或订单簿接口。
- 不把交易所密钥放入前端、Nginx 配置或 Git。
- 本地上线前确认 Nginx 不暴露 `.git`、`.venv`、`references`。
