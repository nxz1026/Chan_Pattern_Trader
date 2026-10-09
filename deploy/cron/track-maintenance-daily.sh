#!/usr/bin/env bash
# 兜底清理「我的追踪」两张表：过期快照（30 天）+ 过期回收站（90 天）。
#
# ## 为什么需要这个脚本（审计 M18：两个 prune 没有执行者）
#
# `cpt/storage/track_store.py` 里的 `prune_snapshots()`（SNAPSHOT_RETENTION_DAYS=30）
# 与 `prune_removed()`（RECYCLE_RETENTION_DAYS=90）此前**零调用方**：
# `cpt/web/track_api.handle_track_maintenance()` 把两者包成一个「日常维护」入口，
# 但它自己的分发没接上 —— `GET /api/dashboard/track/maintenance` 会 404，
# 于是这两张表只涨不消。本脚本按天把它接上。
#
# ## 为什么走 HTTP 而不是像另外两个 prune 脚本那样直连 store
#
# 分发接上后，维护逻辑的**唯一入口**就是那个端点；cron 直连 store 会让
# 「端点没人调」的漂移再次发生（store 侧改个命名/参数，端点静默失效、没人知道）。
# 端点就在本机 loopback：`CPT_HOST=127.0.0.1` + `CPT_PORT=8010`（见
# deploy/env/cpt-dashboard.env），**不经过 nginx ⇒ 不需要任何凭据** ——
# 也就不存在 dashboard-sync.sh 那条「口令进 argv / 跳过 TLS」的问题（R59/审计 M12）。
# ⚠️ 不要把默认的 PUBLIC_URL（https://.../cpt/...）拿来做这件事：那条路径挂了
#    nginx Basic Auth，凭据就得进 cron。
#
# ## ?apply=1 是**必须**的（约定，见审计 M18）
#
# 端点约定：**不带参数 = dry-run（只报计数，不删）；`?apply=1` = 真删**。
# 本脚本显式带 `?apply=1`，并在响应里复核「确实不是 dry-run」——
# 否则一个 dry-run 响应会被当成清理成功，表继续涨而日志一片绿。
#
# ## 时刻：07:30 UTC = 北京 15:30
#
# 非交易时段（A 股 15:00 收盘后半小时，数据写入已沉淀），与仓内其它作业全部错开
# （02:20 / 03:40 / 04:10 / 04:30 / 11:00 UTC），并排在 11:00 UTC 日报之前 ——
# 失败能在**当天**的日报里出现，而不是拖到第二天。
#
# ⚠️ git 里必须带执行位（100755）—— cron 不经过 shell，权限不对直接 Permission denied。
set -uo pipefail

# 与 deploy/dashboard-sync.sh 同一个覆盖变量、同一个默认值。
REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
LOG=/home/ubuntu/logs/track-maintenance.log
LOCK=/home/ubuntu/logs/track-maintenance.lock

# 加载 env：CPT_HOST/CPT_PORT 决定打哪个端口，必须读同一份配置，
# 否则服务挪了端口这里会静默打错地方。缺 env 不拦作业，用下面的默认值。
ENV_FILE=$REPO/deploy/env/cpt-dashboard.env
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
  _ENV_LOADED="yes"
else
  _ENV_LOADED="no"
fi
HOST="${CPT_HOST:-127.0.0.1}"
PORT="${CPT_PORT:-8010}"
URL="http://$HOST:$PORT/api/dashboard/track/maintenance?apply=1"

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮清理还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

{
  echo "===== $(date -u +%FT%TZ) 开始（GET $URL env=$_ENV_LOADED）====="
} >> "$LOG"

# 必须 export：下面是 heredoc 内联的 Python，唯一能带过去的通道就是进程环境。
export CPT_MAINT_URL="$URL"

timeout 2m .venv/bin/python - <<'PY' >> "$LOG" 2>&1
import json
import os
import sys
import urllib.error
import urllib.request

url = os.environ["CPT_MAINT_URL"]
try:
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = resp.read().decode("utf-8", "replace")
        status = resp.status
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", "replace")
    status = exc.code
except Exception as exc:  # noqa: BLE001 - cron 无人盯着，任何异常都要变成非零退出
    raise SystemExit(f"请求维护端点失败：{type(exc).__name__}: {exc}")

print(f"HTTP {status}: {body[:500]}")
if status != 200:
    raise SystemExit(f"维护端点返回 HTTP {status}（分发还没接上？）")

try:
    payload = json.loads(body)
except ValueError:
    raise SystemExit("维护端点返回的不是 JSON")

# ?apply=1 的复核：dry-run 响应绝不能被当成清理成功。
if payload.get("dry_run") is True or payload.get("applied") is False:
    raise SystemExit("端点是 dry-run（没有真删）—— 检查 ?apply=1 是否被识别")
if payload.get("ok") is False:
    raise SystemExit(f"端点上报了失败：{payload.get('error')}")

print(
    f"已删除：快照 {payload.get('pruned_snapshots', '?')} 行 / "
    f"回收站 {payload.get('pruned_removed', '?')} 行"
)
PY
rc=$?

{
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  # 「跑完了」和「跑挂了」不能同码（同 factor-recompute / run-metric 的教训）。
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! 追踪表维护未完成（rc=$rc）—— 过期快照/回收站会继续堆积 !!!!!"
  fi
} >> "$LOG"
exit $rc
