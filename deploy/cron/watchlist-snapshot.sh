#!/usr/bin/env bash
# 只跑「关注列表」的 A 股快照批次：每 15 分钟一次（R80，2026-10-21）。
#
# ## 为什么需要这个脚本
#
# 原有调度只有两条路径让一只票进 `public.cpt_signal_event`：
#
#   1. `cpt-dashboard-ashare.timer` —— 每天 08:00 UTC（16:00 北京）一次，
#      覆盖「热池前 50 + 关注列表」；
#   2. 用户在看板上点开某只票 —— 60 秒内算一次，但**结果不落库**
#      （`GET /api/dashboard/a-share/snapshot` 默认只读，R59 审计 M1），
#      所以全景里仍然没有它。
#
# ⇒ 用户新加一只关注，要等**第二天**才出现在全景里。这是 R80 修掉的问题。
#
# ## 为什么不直接提高主批次的频率
#
# 热池 97 只 × 每 15 分钟 = 一天近万次缠论计算，纯浪费（单只 0.5~2.6 秒）。
# 关注列表通常个位数，一次一两秒。所以**分开**：热池留给每天一次的主批次，
# 关注列表单独高频跑。
#
# ## 界面上「数据跑没跑」不读本脚本的口径
#
# 判据是 `cpt_signal_event` 里这只票**有没有行**（见
# `cpt/application/signal_overview.py::watchlist_status`）。本脚本跑完会写一份
# `~/.cache/cpt/watchlist_batch_status.json` 自述状态，那只是给排障看的 ——
# 文件丢了界面也不会骗人。
#
# ## 空关注列表是**正常态**，不是失败
#
# 用户没关注任何票时脚本 rc=0、什么都不做。别在这里加「无数据即报警」，
# 那会让一个空列表天天报红。
set -uo pipefail

REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
LOG=/home/ubuntu/logs/watchlist-snapshot.log
LOCK=/home/ubuntu/logs/watchlist-snapshot.lock

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

{
  echo "===== $(date -u +%FT%TZ) 开始 ====="
} >> "$LOG"

# 单只最坏 3 秒，留足余量；卡死时让 timeout 收尸，别让下一轮全被 flock 挡掉。
# ⚠️ 不带 PYTHONPATH：R74 修的正是「cron 直接跑脚本时 sys.path[0]=scripts/，
#    依赖 editable 安装把 cpt 指到本仓」——这条路径顺便当门禁⑰的日常回归。
timeout 10m .venv/bin/python scripts/snapshot_a_share_batch.py --only watchlist >> "$LOG" 2>&1
rc=$?

{
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! 关注批次未完成（rc=$rc）—— 新加的关注不会自动出现在全景里 !!!!!"
  fi
} >> "$LOG"
exit $rc
