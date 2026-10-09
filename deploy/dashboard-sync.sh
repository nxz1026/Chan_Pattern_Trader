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
#   2. 源与 bundle 一致性（改了 dash-*.js 却没重建 dashboard.bundle.js —— R59/审计 M14）
#   3. 可选：通过 HTTP 取一次线上文件，确认线上拿到的就是这份
#   4. web 根残留清单（不在预期清单里的顶层条目，只报不删 —— R59/审计 M13）
#
# 用法：
#   deploy/dashboard-sync.sh              # 同步 + 校验
#   deploy/dashboard-sync.sh --verify     # 只校验，不改
#   deploy/dashboard-sync.sh --http       # 额外从 https 取线上文件核对
#     自签证书：优先 CPT_CURL_CA=/path/ca.pem；退而求其次 CPT_CURL_INSECURE=1（会告警）
#
# ⚠️ 刻意**不做** `rm -rf` / `rsync --delete`：`/var/www/cpt-dashboard/_pkg/`
#    是别处放进去的东西，不归本仓管（handoff §7 记过同类坑：远端 git clean -x
#    把 gitignore 挡住的唯一一份飞书 webhook 删了）。替代方案见上面第 4 条
#    残留清单（R59/审计 M13）：只列不删，删除由人拍板。
set -euo pipefail

REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
SRC="$REPO/dashboard"
DEST="${CPT_DASHBOARD_ROOT:-/var/www/cpt-dashboard}"
MODE="sync"
HTTP_CHECK=0
BASIC_AUTH="${CPT_BASIC_AUTH:-admin:ndjack}"
PUBLIC_URL="${CPT_PUBLIC_URL:-https://127.0.0.1/cpt}"

# R59（审计 M12）：HTTP 校验用的凭据**不再走 `curl -u`（会进 argv，同机任何用户
# `ps -ef` 都能看到口令）**，改为写进 mktemp 出来的 curl --config 临时文件
# （0600，EXIT trap 清理）。TLS 校验也默认打开：原先是硬编码 `-k`，
# 等于每次都跳过校验、MITM 也发现不了。自签环境请二选一：
#   CPT_CURL_CA=/path/to/ca.pem   # 首选：指定 CA
#   CPT_CURL_INSECURE=1           # 次选：显式关闭校验（会打印告警）
CURL_CONFIG=""
cleanup_curl_config() {
  [ -n "$CURL_CONFIG" ] && rm -f "$CURL_CONFIG"
}
trap cleanup_curl_config EXIT

# 自签证书处理（见上）。默认严格校验；数组在 set -u 下用安全展开。
CURL_TLS_ARGS=()
if [ -n "${CPT_CURL_CA:-}" ]; then
  CURL_TLS_ARGS+=(--cacert "$CPT_CURL_CA")
elif [ "${CPT_CURL_INSECURE:-0}" = "1" ]; then
  CURL_TLS_ARGS+=(-k)
  echo "  ⚠️ CPT_CURL_INSECURE=1：已显式关闭 TLS 校验（仅限自签环境）" >&2
fi

for arg in "$@"; do
  case "$arg" in
    --verify) MODE="verify" ;;
    --http) HTTP_CHECK=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

# R59（审计 M14）更正：R45 拆分后 **index.html 只加载 dashboard.bundle.js**；
# dash-track.js 由独立页 track.html 单独 `<script src>`。下面列表里的
# dash-core/chrome/structure/signal/chart/alert/ops 是 bundle 的**源文件**，
# 线上浏览器不单独加载它们，同步它们只是为了对照/调试。
# ⚠️ 改了任何 dash-*.js 都必须重建 bundle（见下方「源与 bundle 一致性」检查）。
FILES=(index.html track.html dashboard.css url_safety.js cpt_job.js
       canvas_registry.js canvas_b.js canvas_c.js
       market_a_share.js inspection_panel.js
       dash-core.js dash-chrome.js dash-structure.js dash-signal.js
       dash-chart.js dash-alert.js dash-ops.js dash-track.js
       dashboard.bundle.js)

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

# ── R59（审计 M14）：dash-*.js 源 与 dashboard.bundle.js 一致性 ────────────
# 为什么需要：线上 index.html 只加载 dashboard.bundle.js。只改 dash-core.js 之类
# 的源而不重建 bundle，本脚本的「仓↔部署目录 md5」和「线上 HTTP md5」**都会通过**
# （因为两边推的都是同一个旧 bundle），于是静默上线旧逻辑。复用仓库的
# scripts/build_dashboard_bundle.py --check（它比对 bundle 文本与 build() 输出）。
echo
echo "=== 校验：dash-*.js 源 与 dashboard.bundle.js 一致 ==="
BUNDLE_PY=""
for cand in "$REPO/.venv/bin/python" python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then BUNDLE_PY="$cand"; break; fi
done
BUNDLE_CHECKER="$REPO/scripts/build_dashboard_bundle.py"
if [ -z "$BUNDLE_PY" ]; then
  echo "  ? 找不到 python，跳过（无法确认 bundle 是否是当前源构建的）"
elif [ ! -f "$BUNDLE_CHECKER" ]; then
  echo "  ? 缺少 $BUNDLE_CHECKER，跳过"
elif "$BUNDLE_PY" "$BUNDLE_CHECKER" --check >/dev/null 2>&1; then
  echo "  ✓ 一致（bundle = build(dash-*.js)）"
else
  echo "  ✗ dashboard.bundle.js 与 dash-*.js 不一致 —— 改了源但没重建！"
  echo "    修复：$BUNDLE_PY $BUNDLE_CHECKER   （然后重新跑本脚本）"
  rc=1
fi

# ── R59（审计 M13）：web 根残留清单（**只报不删**）─────────────────────────
# 背景：本脚本刻意不做 rm -rf / rsync --delete（见文件头：_pkg/ 是别的项目放的）。
# 代价是 web 根会长期堆积仓库外产物 —— 实测有 *.bak-*、root 下的 run*.sh、
# collector-cn-*.tgz（后者是 league/collector-cn 采集机整包，含内部拓扑，
# 放在公开 web 根等于可被遍历下载）。这里只把「不在预期清单里」的**顶层**条目
# 列出来告警，不删除；删除由人拍板（先 cp 到 /tmp 备份再删）。
# 预期清单（也写在 deploy/README.md「web 根允许的文件」）：
#   - 本脚本 FILES 里的每个文件（顶层）
#   - vendor/（随 dashboard/ 一起同步）
#   - _pkg/（外部项目的发布通道，不归本仓管）
echo
echo "=== 残留检查：$DEST 顶层不在预期清单中的条目（只报不删）==="
residual=()
if [ -d "$DEST" ]; then
  while IFS= read -r entry; do
    base=$(basename "$entry")
    case "$base" in
      vendor|_pkg) continue ;;   # 预期目录，跳过
    esac
    if [ -d "$entry" ]; then
      residual+=("$base/  （预期外目录）")
      continue
    fi
    hit=0
    for f in "${FILES[@]}"; do
      if [ "$base" = "$f" ]; then hit=1; break; fi
    done
    [ "$hit" -eq 0 ] && residual+=("$base")
  done < <(find "$DEST" -mindepth 1 -maxdepth 1)
fi
if [ "${#residual[@]}" -eq 0 ]; then
  echo "  ✓ 无预期外条目"
else
  echo "  ⚠️ 发现 ${#residual[@]} 个预期外条目（不自动删除）："
  for r in "${residual[@]}"; do
    echo "      - $r"
  done
  echo "    处置：先 cp 到 /tmp 备份再人工删除；确认 _pkg/ 内文件无敏感信息。"
fi

if [ "$HTTP_CHECK" -eq 1 ]; then
  echo
  echo "=== 校验：线上 HTTP 拿到的内容 ==="
  # R59（审计 M12）：凭据写进 0600 临时 config，不进 argv；TLS 按 CURL_TLS_ARGS。
  CURL_CONFIG=$(umask 077; mktemp "${TMPDIR:-/tmp}/cpt-curl-XXXXXX")
  printf 'user = "%s"\n' "$BASIC_AUTH" > "$CURL_CONFIG"
  HTTP_BODY=$(mktemp "${TMPDIR:-/tmp}/cpt-http-XXXXXX")
  for f in dashboard.bundle.js index.html dashboard.css; do
    local_url="$PUBLIC_URL/$f"
    if curl -s --config "$CURL_CONFIG" ${CURL_TLS_ARGS[@]+"${CURL_TLS_ARGS[@]}"} \
         --max-time 15 "$local_url" -o "$HTTP_BODY" 2>/dev/null; then
      a=$(md5sum "$DEST/$f" | cut -d' ' -f1)
      b=$(md5sum "$HTTP_BODY" | cut -d' ' -f1)
      if [ "$a" = "$b" ]; then
        echo "  ✓ $f 线上 = 部署目录"
      else
        echo "  ✗ $f 线上与部署目录不一致 —— 检查 nginx 缓存 / alias 路径"
        rc=1
      fi
    else
      echo "  ? $f 取不到（服务没起？或自签证书需 CPT_CURL_CA / CPT_CURL_INSECURE=1）"
    fi
  done
  rm -f "$HTTP_BODY"
fi

echo
if [ "$rc" -eq 0 ]; then
  echo "✅ 全部一致"
else
  echo "❌ 有不一致 —— 线上跑的不是这份代码"
fi
exit $rc
