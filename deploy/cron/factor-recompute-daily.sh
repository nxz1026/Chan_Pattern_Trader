#!/usr/bin/env bash
# 每日一轮：按公司行动重算后复权因子（**只写暂存表 asel.ref_adjust_factor_v2**，
# 不碰生产表 asel.ref_adjust_factor —— 切换与否看 scripts/factor_report.py 的报告）。
#
# 安装：直接装仓内那份 crontab（**不要**手工 echo 一行，见 deploy/cron/crontab）：
#   crontab deploy/cron/crontab
#
# ## 为什么不复制到 /home/ubuntu/bin/（R45 决策）
#
# 原来这里写的是 ``install -m 755 <脚本> /home/ubuntu/bin/``，crontab 指向那份**副本**。
# 问题不在能不能跑（当时两份是一致的），而在**没有任何机制保证它们继续一致** ——
# 改了仓内脚本而忘了 scp，线上就跑旧版，**且没有任何检查会报警**。
# 「两份文件靠人记得同步」本身就是这个问题的根源。
#
# 现在 crontab **直接指向仓内路径**：
#   - 仓内脚本是 git 跟踪的（``git clean`` 只删未跟踪文件，删不掉它们）；
#   - 三个脚本都自带 ``cd "$REPO"``，**不依赖 cron 给的 CWD**（cron 给的是 ``$HOME``）；
#   - 解释器走仓内 ``.venv/bin/python``，与手动跑完全一致。
#
# ⚠️ **代价**（明说，别以为没有）：仓被 ``git checkout`` 到旧提交时，
# 跑的就是旧脚本 —— 但这本来就是 git 该有的行为，而且**改代码却不同步线上**
# 本来就不该发生。相比「默默跑一个过期副本」，这个代价小得多。
#
# ⚠️ git 里必须带执行位（``100755``）—— cron 不经过 shell，
# 权限不对会直接 ``Permission denied``。R45 实测：另两个脚本当时是 ``100644``。
set -uo pipefail

# 与 deploy/dashboard-sync.sh 同一个覆盖变量、同一个默认值 ——
# 三个 cron 脚本此前各自硬编码 REPO，改了默认路径就得改三处，漏一处就静默跑错目录。
REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
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
