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

`deploy/systemd/cpt-dashboard.service` 是模板。安装前**先创建环境文件**，否则服务会
拒绝启动（模板里的 `EnvironmentFile=` 故意不带 `-` 前缀，文件缺失时直接报
`Failed to load environment files`，而不是静默把参数展开成空串）：

```bash
cp deploy/env/cpt-dashboard.env.example deploy/env/cpt-dashboard.env
sudo cp deploy/systemd/cpt-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cpt-dashboard
sudo systemctl status cpt-dashboard
```

`--mode realtime` **已可用**（真实 engine snapshot provider 已接线）：线上服务就是
`--mode realtime --poll-seconds 30`，`/api/dashboard/health` 返回
`ok=true / degraded=false`。需要空数据调试时才用 `CPT_MODE=demo`；不要把 demo 的
空 snapshot 当作真实行情。

## 安全边界

- API 只读，不提供下单、撤单、账户、持仓或订单簿接口。
- 不把交易所密钥放入前端、Nginx 配置或 Git。
- 本地上线前确认 Nginx 不暴露 `.git`、`.venv`、`references`。
