#!/usr/bin/env bash
# 每日巡检：读 public.cpt_run_metric + 数据源状态 → 状态变化或出现 failing 时发飞书。
#
# 安装（oracle，用户级 crontab）—— **不需要复制脚本**：
#   ( crontab -l | grep -v run-inspection-daily.sh; \
#     echo '40 3 * * * /home/ubuntu/DSH/Chan_Pattern_Trader/deploy/cron/run-inspection-daily.sh >> /home/ubuntu/logs/run-inspection.log 2>&1' ) | crontab -
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
