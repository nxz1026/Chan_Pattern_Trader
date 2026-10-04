#!/usr/bin/env bash
# 保留策略：清理 public.cpt_run_metric 里超出窗口的**巡检**行。
#
# ## 为什么需要这个脚本
#
# ``cpt/storage/run_metric_store.prune`` 一直存在却**零调用方**（R45 storage
# 复盘实测：全仓含测试 grep 无引用）—— 也就是说这张表**从来没被清理过**，
# 会按「每轮一行」的速度无界增长。函数自己的 docstring 写着：
#
#     run 行是高频的（每轮一行），不留窗口就会长成第二份 ``daily_bar``。
#
# 2026-10-03 实测当时才 2992 行 / 1.9 MB，不急；但巡检是每天都在写的，
# 放着不管就是一条确定的慢性泄漏。
#
# ⚠️ **只删 run 类行，不碰 inspection 类行**：``cpt_run_metric`` 一张表里
# 混了两类（``KIND_RUN`` / ``KIND_INSPECTION``），巡检行是每天状态变化比对的
# 依据，窗口短得多。所以按 kind 过滤，且 retention 分开配。
#
# 安装（oracle，用户级 crontab）—— **不需要复制脚本**：
#   ( crontab -l | grep -v run-metric-prune-daily.sh; \
#     echo '10 4 * * * /home/ubuntu/DSH/Chan_Pattern_Trader/deploy/cron/run-metric-prune-daily.sh >> /home/ubuntu/logs/run-metric-prune.log 2>&1' ) | crontab -
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
LOG=/home/ubuntu/logs/run-metric-prune.log
LOCK=/home/ubuntu/logs/run-metric-prune.lock
#: run 行保留天数。行情类行留 90 天够做同比；再久就没意义了（因子表才 2.75 年）。
KEEP_RUN_DAYS="${CPT_PRUNE_RUN_DAYS:-90}"

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮清理还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

{
  echo "===== $(date -u +%FT%TZ) 开始（保留 run 行 $KEEP_RUN_DAYS 天）====="
} >> "$LOG"

# ⚠️ store 层不 commit（事务边界归调用方，见 cpt/storage/__init__.py）——
#    所以这里必须自己 commit，漏了就是静默回滚、删了跟没删一样。
timeout 5m .venv/bin/python - <<'PY' >> "$LOG" 2>&1
import sys
sys.path.insert(0, ".")
from cpt.adapters.a_share_local import AShareLocalClient
from cpt.storage.run_metric_store import KIND_INSPECTION, KIND_RUN, prune

client = AShareLocalClient()
try:
    conn = client._get_conn()
    # 分开配窗口：run 行是每轮一行的流水，inspection 是每天一次的状态比对。
    # prune 失败会抛（R45：失败与「没东西可删」不能同码），所以异常会冒到下面。
    n_run = prune(conn, keep_days=90, kinds=[KIND_RUN])
    n_insp = prune(conn, keep_days=30, kinds=[KIND_INSPECTION])
    conn.commit()          # ⚠️ store 层不 commit，漏了就是静默回滚
    print(f"已删除 run 行 {n_run}（>90 天）、inspection 行 {n_insp}（>30 天）")
    if n_run + n_insp == 0:
        print("  （0 行 = 本来就没有过期数据，不是失败）")
except Exception as exc:
    print(f"清理失败 {type(exc).__name__}: {exc}")
    raise
finally:
    client.close()
PY
rc=$?

{
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  # 与 factor-recompute 同一个教训（R45）：「跑完了」和「跑挂了」不能同码。
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! 清理未完成（rc=$rc）—— run 类行会继续堆积 !!!!!"
  fi
} >> "$LOG"
exit $rc
