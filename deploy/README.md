# CPT 本地 Dashboard 部署

## Demo API

在仓库根目录执行：

```bash
cp deploy/env/cpt-dashboard.env.example deploy/env/cpt-dashboard.env
.venv/bin/python -m cpt.web --host 127.0.0.1 --port 8000 --mode demo
```

验证：

```bash
curl http://127.0.0.1:8000/api/dashboard/health
curl http://127.0.0.1:8000/api/dashboard/snapshot
```

## Nginx

将 `deploy/nginx/cpt-dashboard.conf` 安装到 Nginx 的 `sites-enabled`（该文件是 `http {}` 内的 `server` 片段，不是独立 nginx.conf），确认其中的仓库路径和服务端口正确，然后执行 `nginx -t` 并 reload。Nginx 仅提供静态 Dashboard 和 `/api/` 反向代理；Python API 必须先运行。当前环境已用临时 Nginx 实例验证静态页和 API 代理。

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

`deploy/systemd/cpt-dashboard.service` 是模板，安装前检查 `User`、工作目录、Python 路径和环境文件：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cpt-dashboard
sudo systemctl status cpt-dashboard
```

当前 `--mode realtime` 会明确拒绝启动，因为真实 engine snapshot provider 尚未接线；不要把 demo 空 snapshot 当作真实行情。

## 安全边界

- API 只读，不提供下单、撤单、账户、持仓或订单簿接口。
- 不把交易所密钥放入前端、Nginx 配置或 Git。
- 本地上线前确认 Nginx 不暴露 `.git`、`.venv`、`references`。
