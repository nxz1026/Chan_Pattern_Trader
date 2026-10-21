#!/usr/bin/env bash
# 观察期巡检（R83）：每 2 小时问一次「系统还在转吗」。
#
# ## 为什么需要它（R83 的由来）
#
# R82 之后 CPT 进入观察期。但「观察期」不是一个状态，是一个**要有东西在看**的动作：
# run_inspection.py 只回答「K 线/因子/结构算得对不对」，它对**服务进程、序列、
# 批次、盘口通道**全都不看 —— 而 CPT 历史上恰好在这些地方出过事：
#
#   - cpt-dashboard.service 停过 55 小时（Restart=on-failure 不覆盖 SIGTERM 正常退出）
#   - bigserial 序列落后四次，靠手工/自愈修，日志里只有一行 warning 没人看
#   - watchlist-daily-summary 连续两天 ModuleNotFoundError（editable 安装指向已删目录）
#
# 这三条没有一条会被 run_inspection.py 抓到。所以观察期必须有**另一双眼睛**。
#
# ## 为什么是 2 小时
#
# 服务死了要在**有人发现之前**知道。2 小时意味着最坏情况丢半天；再密就是
# 每小时一条心跳噪音（watchlist-snapshot 已经每 15 分钟在跑，但它不报警）。
# 本脚本只在**状态变了**时发飞书，所以频率高不等于吵。
#
# ## 为什么 heredoc 里带 `set -uo pipefail`
#
# 与其它 cron 脚本一致：`u` 让拼错的变量立刻炸，`pipefail` 让管道中间的失败
# 不被最后的 `tail` 吃掉。不开 `-e`：要拿到 rc 再决定记什么。
set -uo pipefail

REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
ENV_FILE=$REPO/deploy/env/cpt-dashboard.env
LOG=/home/ubuntu/logs/watch-period-check.log
LOCK=/home/ubuntu/logs/watch-period-check.lock

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

# ⚠️ 必须 source env —— 否则 CPT_FEISHU_WEBHOOK 不在环境里，巡检只会往日志写
# 「未配置」，一条告警也发不出去（run-inspection-daily.sh 踩过同一个坑，R56）。
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
else
  echo "[$(date -u +%FT%TZ)] 找不到 $ENV_FILE，飞书告警不可用" >&2
  echo "[$(date -u +%FT%TZ)] 找不到 $ENV_FILE，飞书告警不可用" >> "$LOG"
  exit 78
fi

# ⚠️ 不带 PYTHONPATH：editable 安装已修（R74），顺带当门禁⑰的每日回归。
# 超时必须留：网络/DB 挂住时不能让这一轮永远不结束。
{
  echo "===== $(date -u +%FT%TZ) 开始 ====="
} >> "$LOG"
timeout 5m .venv/bin/python scripts/watch_period_check.py >> "$LOG" 2>&1
rc=$?
{
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! rc=$rc（1=有问题 78=告警通道不可用）!!!!!"
  fi
} >> "$LOG"
exit $rc
