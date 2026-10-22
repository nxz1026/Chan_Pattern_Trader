#!/usr/bin/env bash
# 盘中采集的一轮（R89）。**由 systemd timer 拉起，不挂在 cron 上。**
#
# ## 为什么这个脚本仍然存在（如果 timer 直接调 python）
#
# timer 的 ExecStart 已经直接调 `.venv/bin/python scripts/intraday_collectd.py`，
# 本脚本是**手动/排障入口**：带上 flock、日志落盘、rc 归一。挂着排查线上
# 问题时会直接跑它。
#
# ## 为什么不用 cron 而用 systemd timer
#
# 现有 8 条 CPT cron 都是「每天一次」的长作业，用 cron 合适。本作业是
# **交易时段每 2 分钟一次**并且**跨午休要停**，cron 写 `*/2 1,2,5,6 * * 1-5`
# 会在 11:32~13:00 与 15:00 之后多跑一堆空轮；systemd timer 的
# `OnCalendar=*-*-* 09:30/2:00 Asia/Shanghai` 直接把时段与午休写进表达式，
# 而且 `systemd-analyze calendar` 能预演 —— 比 cron 可验证。
set -uo pipefail

REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
LOG=/home/ubuntu/logs/intraday-collect.log
LOCK=/home/ubuntu/logs/intraday-collect.lock

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

# ⚠️ 不带 PYTHONPATH：这条路径顺便当门禁⑰（editable 安装）的日常回归。
timeout 90 .venv/bin/python scripts/intraday_collectd.py "${@:---once}" >> "$LOG" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "[$(date -u +%FT%TZ)] !!!!! 采集 rc=$rc !!!!!" >> "$LOG"
fi
exit $rc
