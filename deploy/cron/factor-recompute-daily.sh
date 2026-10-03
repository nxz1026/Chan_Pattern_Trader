#!/usr/bin/env bash
# 每日一轮：按公司行动重算后复权因子（**只写暂存表 asel.ref_adjust_factor_v2**，
# 不碰生产表 asel.ref_adjust_factor —— 切换与否看 scripts/factor_report.py 的报告）。
#
# 安装（oracle，用户级 crontab）：
#   install -m 755 deploy/cron/factor-recompute-daily.sh /home/ubuntu/bin/
#   ( crontab -l | grep -v factor-recompute-daily.sh; \
#     echo '20 2 * * * /home/ubuntu/bin/factor-recompute-daily.sh >> /home/ubuntu/logs/factor-recompute.log 2>&1' ) | crontab -
#
# 02:20 UTC = 北京时间 10:20。
set -uo pipefail

REPO=/home/ubuntu/DSH/Chan_Pattern_Trader
LOG=/home/ubuntu/logs/factor-recompute.log
LOCK=/home/ubuntu/logs/factor-recompute.lock

# 真值源：默认东财（免费、无额度）。用 CPT_RECOMPUTE_SOURCE=wind 切回 Wind 做交叉校验。
SOURCE="${CPT_RECOMPUTE_SOURCE:-eastmoney}"
# 单轮处理上限。⚠️ 东财无额度，这只是「别让一轮跑太久」的时间预算；
# 用 wind 时它才重新变成真正的硬边界，此时应调小。
MAX_CALLS="${CPT_RECOMPUTE_MAX_CALLS:-6000}"
# 候选集：placeholder = 优先补从没算过的（source IS NULL）；all = 所有有 bar 的
SCOPE="${CPT_RECOMPUTE_SCOPE:-placeholder}"

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1
# Wind 的 node 装在 nvm 下，cron 的 PATH 里没有；只有 --source wind 时才用得上。
export CPT_WIND_NODE="${CPT_WIND_NODE:-/home/ubuntu/.local/bin/node}"

{
  echo "===== $(date -u +%FT%TZ) 开始（源=$SOURCE scope=$SCOPE 上限 $MAX_CALLS）====="
} >> "$LOG"

timeout 10h .venv/bin/python scripts/factor_recompute.py \
  --source "$SOURCE" --scope "$SCOPE" --max-calls "$MAX_CALLS" >> "$LOG" 2>&1
rc=$?

{
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  .venv/bin/python scripts/factor_recompute.py --report 2>&1 | head -20
  # ⚠️ R44：rc=3 = 本轮被通道/额度类错误打断（脚本侧「跑完了」和「跑死了」同码）。
  # 实测 2026-10-02：13:47 开始、13:50 结束、rc=0 —— 一次 3 分钟就死的任务
  # 被记成了成功。这行让「死过」在日志里**可 grep**，外部看门狗也能据此告警。
  if [ "$rc" -eq 3 ]; then
    echo "!!!!! 本轮未完成（rc=3，被打断）：进度已存盘，下次自动续跑 !!!!!"
  fi
} >> "$LOG"
exit $rc
