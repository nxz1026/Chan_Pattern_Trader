#!/usr/bin/env bash
# 每日作业日报：扫四条 cron 作业的日志，发现失败就发飞书。
#
# 安装：直接装仓内那份 crontab（**不要**手工 echo 一行，见 deploy/cron/crontab）：
#   crontab deploy/cron/crontab
#
# ## 为什么需要这个脚本（R57 上线前审计结论）
#
# 四条作业失败时的行为是「非零退出 + 往日志里打一行 `!!!!!`」，然后**就没有然后了**：
#
# - crontab 里没有 ``MAILTO``；
# - 全仓没有 logrotate；
# - **没有任何 watchdog / 消费者去 grep 这些日志**。
#
# 唯一有出口的是 ``run-inspection-daily.sh`` 的飞书，而它只覆盖**业务巡检自己的
# 结论**（水位、数据源状态），完全不看另外三条作业的死活。
#
# ⇒ 因子重算或清理失败 = 表继续膨胀 / 数据继续陈旧，而**外部毫无异常**。
# 这与 R23 那条「保留期只写在注释里、从没自动化」是同一个病：
# **做了记录 ≠ 有人会看。**
#
# ## 为什么排在 07:00 而不是紧跟每条作业
#
# 紧跟会造成「一条作业失败 → 日报立刻报 → 另一个人手工重跑 → 日报再报一次」。
# 早晨汇总的好处是：日报里的每一条都已经过了完整的重试窗口仍不成立，
# 那才是真需要人看的东西。
#
# ⚠️ git 里必须带执行位（``100755``）—— cron 不经过 shell，
# 权限不对会直接 ``Permission denied``。
set -uo pipefail

REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
ENV_FILE=$REPO/deploy/env/cpt-dashboard.env
LOG=/home/ubuntu/logs/cron-daily-report.log
LOCK=/home/ubuntu/logs/cron-daily-report.lock
#: 回看窗口（小时）。作业集中在 02:20~04:30，24 小时足够覆盖「昨天跑没跑」。
WINDOW_HOURS="${CPT_REPORT_WINDOW_HOURS:-24}"

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮日报还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

# ⚠️ 必须加载 env —— 否则 CPT_FEISHU_WEBHOOK 不在环境里，日报只会往日志里写
# 「未配置」，一条告警也发不出去，而且**报告成功**（与 R56 修巡检时同一个病：
# 告警通道自己坏了却报成功，是最坏的一种失效）。
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
else
  echo "[$(date -u +%FT%TZ)] 找不到 $ENV_FILE，飞书告警不可用" >&2
  echo "[$(date -u +%FT%TZ)] 找不到 $ENV_FILE，飞书告警不可用" >> "$LOG"
  exit 78   # EX_CONFIG：配置缺失，与「跑挂了」区分开
fi

: "${CPT_FEISHU_WEBHOOK:?CPT_FEISHU_WEBHOOK 未设置 —— 日报将无法告警，退出}"
echo "[$(date -u +%FT%TZ)] 日报开始（回看 ${WINDOW_HOURS} 小时）" >> "$LOG"

export WINDOW_HOURS

# ⚠️ timeout：没有它，一旦日志极大或网络挂住，这一轮会永远挂着，
# 而 cron 会在明天再起一个 ⇒ 并发叠加（与 run-inspection 同款理由）。
timeout 10m .venv/bin/python scripts/cron_daily_report.py --hours "$WINDOW_HOURS" \
  >> "$LOG" 2>&1
rc=$?

{
  echo "===== $(date -u +%FT%TZ) 日报结束 rc=$rc ====="
  # 与另三条脚本同一个纪律：「跑完了」和「跑挂了」不能同码。
  # cron_daily_report.py 的退出码语义：
  #   0 = 扫完了（无论有没有发现问题）
  #   3 = 有问题但 webhook 没配
  #   4 = 有问题但飞书没送达
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! 日报未完成（rc=$rc）—— 作业失败可能仍然没人知道 !!!!!"
  fi
} >> "$LOG"
exit $rc