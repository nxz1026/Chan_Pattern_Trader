#!/usr/bin/env bash
# 同步看板静态资源到 nginx 静态根，**并校验**。
#
# ## 为什么需要这个脚本
#
# nginx 的 `location /cpt/ { alias /var/www/cpt-dashboard/; }`（见
# deploy/nginx/cpt-dashboard.conf）指向的是**独立部署副本**，不是仓库目录。
# 两者各改各的：只 scp 到 ~/DSH/Chan_Pattern_Trader/dashboard/ 的话，
# **线上毫无变化**，而你毫不知情。
#
# R44 实测：为此白截了两张图才发现改动没上线。deploy/README.md 里
# 原本只写了手工 `cp`，没有校验，于是「以为部署了」和「真部署了」长得一样。
#
# 所以本脚本的重点不是 cp，是**部署后校验**：
#   1. 逐文件比 md5（部署目录 vs 仓库）
#   2. 可选：通过 HTTP 取一次线上文件，确认线上拿到的就是这份
#
# 用法：
#   deploy/dashboard-sync.sh              # 同步 + 校验
#   deploy/dashboard-sync.sh --verify     # 只校验，不改
#   deploy/dashboard-sync.sh --http       # 额外从 https 取线上文件核对
#
# ⚠️ 刻意**不做** `rm -rf` / `rsync --delete`：`/var/www/cpt-dashboard/_pkg/`
#    是别处放进去的东西，不归本仓管（handoff §7 记过同类坑：远端 git clean -x
#    把 gitignore 挡住的唯一一份飞书 webhook 删了）。
set -euo pipefail

REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
SRC="$REPO/dashboard"
DEST="${CPT_DASHBOARD_ROOT:-/var/www/cpt-dashboard}"
MODE="sync"
HTTP_CHECK=0
BASIC_AUTH="${CPT_BASIC_AUTH:-admin:ndjack}"
PUBLIC_URL="${CPT_PUBLIC_URL:-https://127.0.0.1/cpt}"

for arg in "$@"; do
  case "$arg" in
    --verify) MODE="verify" ;;
    --http) HTTP_CHECK=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

FILES=(index.html dashboard.css dashboard.js canvas_b.js canvas_c.js canvas_d.js
       canvas_registry.js market_a_share.js inspection_panel.js url_safety.js)

[ -d "$SRC" ] || { echo "仓库里没有 $SRC" >&2; exit 1; }
[ -d "$DEST" ] || { echo "部署目录不存在: $DEST" >&2; exit 1; }

if [ "$MODE" = "sync" ]; then
  echo "=== 同步 $SRC -> $DEST ==="
  for f in "${FILES[@]}"; do
    [ -f "$SRC/$f" ] || { echo "  跳过（仓库里没有）: $f"; continue; }
    # ⚠️ CRLF 归一化（handoff §7：从 Windows scp 上去的文件带 \r，
    #    仓/服务器规范是 LF，不归一化线上与仓不一致）
    tr -d '\r' < "$SRC/$f" > "$DEST/$f"
    chmod 644 "$DEST/$f"
    echo "  ✓ $f"
  done
  if [ -d "$SRC/vendor" ]; then
    mkdir -p "$DEST/vendor"
    cp -r "$SRC/vendor/." "$DEST/vendor/"
    echo "  ✓ vendor/"
  fi
  sudo -n chown -R ubuntu:ubuntu "$DEST" 2>/dev/null || true
fi

echo
echo "=== 校验：部署目录 vs 仓库 ==="
rc=0
for f in "${FILES[@]}"; do
  [ -f "$SRC/$f" ] || continue
  a=$(tr -d '\r' < "$SRC/$f" | md5sum | cut -d' ' -f1)
  b=$(md5sum "$DEST/$f" 2>/dev/null | cut -d' ' -f1 || echo "-")
  if [ "$a" = "$b" ]; then
    echo "  ✓ $f"
  else
    echo "  ✗ $f  仓=${a:0:8} 部署=${b:0:8}   ← 不一致！线上还是旧的"
    rc=1
  fi
done

if [ "$HTTP_CHECK" -eq 1 ]; then
  echo
  echo "=== 校验：线上 HTTP 拿到的内容 ==="
  for f in dashboard.js index.html dashboard.css; do
    local_url="$PUBLIC_URL/$f"
    if curl -sk -u "$BASIC_AUTH" --max-time 15 "$local_url" -o /tmp/_cpt_http_body 2>/dev/null; then
      a=$(md5sum "$DEST/$f" | cut -d' ' -f1)
      b=$(md5sum /tmp/_cpt_http_body | cut -d' ' -f1)
      if [ "$a" = "$b" ]; then
        echo "  ✓ $f 线上 = 部署目录"
      else
        echo "  ✗ $f 线上与部署目录不一致 —— 检查 nginx 缓存 / alias 路径"
        rc=1
      fi
    else
      echo "  ? $f 取不到（服务没起？）"
    fi
  done
  rm -f /tmp/_cpt_http_body
fi

echo
if [ "$rc" -eq 0 ]; then
  echo "✅ 全部一致"
else
  echo "❌ 有不一致 —— 线上跑的不是这份代码"
fi
exit $rc
