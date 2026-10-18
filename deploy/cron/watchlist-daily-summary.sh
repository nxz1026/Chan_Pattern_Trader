#!/usr/bin/env bash
# 每个交易日推一条「关注列表收盘汇总」（R4/C2 ④）。
#
# ## 与 cron-daily-report.sh 的分工
#
# 日报**只在有故障时**才推（静默日 print 一行就 return 0）；这条是**产品功能**，
# 每个交易日都推一次。两者的失败出口相同（都走飞书、都在 scripts/cron_daily_report.py
# 的 JOBS 里扫日志），所以它挂了会在当天的日报里出现。
#
# ## 为什么放在 10:10 UTC = 北京 18:10
#
# 时刻被上游决定（同日报那条 R57 踩坑）：快照与共享表依赖 emotion-core 的 pipeline
# （北京时间 17:20，emotion-core-daily.timer）与它的 18:05 watchdog。早于这两者跑，
# 拿到的是当天不完整的数据 —— 那种「看着像真的、其实是旧数」的推送比不推更糟。
# 同时必须早于 11:00 UTC 的日报，好让失败在**当天**可见。
#
# ## 脚本自己会先刷快照
#
# `cpt_track_snapshot` 只在有人打开追踪页 / 主动拉建议时才写。这里固定刷新一次
# （loopback `GET /api/dashboard/track/<code>/advice` + `X-CPT-User`）以保证播报的是
# 当天价位，而不是昨天甚至更早。见 scripts/watchlist_daily_summary.py。
#
# ⚠️ git 里必须带执行位（100755）—— cron 不经过 shell，权限不对直接 Permission denied。
set -uo pipefail

# 与 deploy/dashboard-sync.sh 同一个覆盖变量、同一个默认值。
REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
ENV_FILE=$REPO/deploy/env/cpt-dashboard.env
LOG=/home/ubuntu/logs/watchlist-daily-summary.log
LOCK=/home/ubuntu/logs/watchlist-daily-summary.lock

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮汇总还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

# 加载 env：CPT_FEISHU_WEBHOOK 决定能不能送达。缺文件不拦作业 ——
# 脚本会以 rc=3（未送达）报出来，日志里看得见。
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
  _ENV_LOADED="yes"
else
  _ENV_LOADED="no"
fi

echo "===== $(date -u +%FT%TZ) 开始（关注列表收盘汇总 env=$_ENV_LOADED）=====" >> "$LOG"

timeout 10m .venv/bin/python scripts/watchlist_daily_summary.py >> "$LOG" 2>&1
rc=$?

{
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  # 「跑完了」和「跑挂了」不能同码（同 factor-recompute / track-maintenance 的教训）。
  # rc=5 是「送达了但价位可能过期」—— 与「没推送」必须分开说，日志不许撒谎。
  if [ "$rc" -eq 5 ]; then
    echo "!!!!! 关注列表收盘汇总：快照刷新全量失败，推的可能是过期价位（rc=5）!!!!!"
  elif [ "$rc" -ne 0 ]; then
    echo "!!!!! 关注列表收盘汇总未完成（rc=$rc）—— 当天没有汇总推送 !!!!!"
  fi
} >> "$LOG"
exit $rc
