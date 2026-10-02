#!/usr/bin/env bash
# 每日巡检：读 public.cpt_run_metric + 数据源状态 → 状态变化或出现 failing 时发飞书。
#
# 安装（oracle，用户级 crontab）：
#   install -m 755 deploy/cron/run-inspection-daily.sh /home/ubuntu/bin/
#   ( crontab -l | grep -v run-inspection-daily.sh; \
#     echo '40 3 * * * /home/ubuntu/bin/run-inspection-daily.sh >> /home/ubuntu/logs/run-inspection.log 2>&1' ) | crontab -
#
# 03:40 UTC = 北京时间 11:40，刻意避开 02:20 的因子重算与 08:00 的 A 股快照。
set -uo pipefail

REPO=/home/ubuntu/DSH/Chan_Pattern_Trader
ENV_FILE=$REPO/deploy/env/cpt-dashboard.env
cd "$REPO" || exit 1

# ⚠️ 必须加载 env —— 否则 CPT_FEISHU_WEBHOOK 不在环境里，每天的巡检只会往
# 日志里写「未配置」，一条告警也发不出去。webhook 只存在于这个 gitignore 掉的
# 文件里（systemd 那边是 EnvironmentFile=，cron 这边没有等价物，得自己 source）。
# 用 set -a/-al 只导出、不改 shell 语义。
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
else
  echo "[$(date -u +%FT%TZ)] 找不到 $ENV_FILE，飞书告警不可用" >&2
fi

exec .venv/bin/python scripts/run_inspection.py
