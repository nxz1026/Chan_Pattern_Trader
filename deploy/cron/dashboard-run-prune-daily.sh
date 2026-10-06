#!/usr/bin/env bash
# 保留策略：清理 public.cpt_dashboard_run 里超出窗口的行。
#
# ## 为什么需要这个脚本
#
# R23 建表时的结论是「append-only + **不自动 GC**」，理由写得很清楚：
# 「避免在 HTTP 请求路径上跑大 SQL」，并把手工清理的 SQL 留在迁移注释里——
#
#     DELETE FROM public.cpt_dashboard_run WHERE generated_at < now() - interval '7 days'
#
# 2026-10-06 实测：**这条注释从来没变成过任何自动化**。全仓 grep 无
# retention / cleanup / purge 逻辑（唯一一条 DELETE 是 cpt_run_metric 的 prune），
# 生产 crontab 三条 CPT 作业里也没有这条 —— 这张表**从来没被清理过**。
#
# 当时的量：60 行 / 4648 kB，**约 79 kB/行**（snapshot jsonb 很肥）。
# 增长由**数据变化**驱动而非轮询：`record_run` 对 30s 内同 `(run_id, dataset_hash)`
# 去重，命中缓存根本不写库（实测 45 秒 0 新增）。粗算 ~300 行/天 ≈ 24 MB/天，
# 一年 ≈ 9 GB —— 慢，但方向确定。
#
# 清理放**离线 cron**、不放 HTTP 路径：R23 当初那个顾虑是对的，错的只是
# 「以为留个注释就够了」。
#
# ## 为什么键是 COALESCE(generated_at, created_at)
#
# R56 放开 generated_at 的 NOT NULL 后，裸 `generated_at < ...` **永远命中不了
# NULL 行**（SQL 三值逻辑），那批行会永久留存。R57 补了 created_at（入库时刻，
# 数据库记的，不可能是业务伪造的），COALESCE 既覆盖 NULL 行，又命中
# `idx_cpt_dashboard_run_coalesce_time` 索引。详见
# cpt/storage/dashboard_run_store.py::prune 的 docstring。
#
# ## 为什么不复制到 /home/ubuntu/bin/（R45 决策，与 run-metric 同理）
#
# crontab **直接指向仓内路径**：仓内脚本是 git 跟踪的，三个脚本都自带 `cd "$REPO"`
# 不依赖 cron 给的 CWD，解释器走仓内 .venv/bin/python 与手动跑完全一致。
# 「两份文件靠人记得同步」本身就是问题的根源。代价：仓被 checkout 到旧提交时跑的
# 就是旧脚本 —— 但改代码不同步线上本来就不该发生。
#
# ⚠️ git 里必须带执行位（100755）—— cron 不经过 shell，权限不对直接 Permission denied。
set -uo pipefail

# 与 deploy/dashboard-sync.sh 同一个覆盖变量、同一个默认值 —— 各 cron 脚本
# 此前各自硬编码 REPO，改了默认路径就得改多处，漏一处就静默跑错目录。
REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
LOG=/home/ubuntu/logs/dashboard-run-prune.log
LOCK=/home/ubuntu/logs/dashboard-run-prune.lock

# ⚠️ R57（2026-10-06 上线前审计补）：加载 env 文件。
# 原因与代价见 factor-recompute-daily.sh 的同名注释：原来只有巡检脚本 source
# 它，所以把 `CPT_PRUNE_DASHBOARD_RUN_DAYS` 写进 env 是无声无效的。
# **必须插在下面 `KEEP_DAYS="${...:-7}"` 赋值之前**，否则 env 里的值读不到。
# 与巡检不同：缺 env 不拦作业（只是用默认 7 天）。
ENV_FILE=$REPO/deploy/env/cpt-dashboard.env
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
  _ENV_LOADED="yes"
else
  _ENV_LOADED="no"
fi
#: 保留天数。R23 迁移注释写的是 7 天，与之保持一致。
#: 依据：这张表的用途是「跨重启可比」，7 天约 2000 轮，远超 ring 的 50 条，
#: 够做任意两次运行对比；而按实测 ~79 kB/行，7 天约 170 MB，是可以接受的常数。
KEEP_DAYS="${CPT_PRUNE_DASHBOARD_RUN_DAYS:-7}"

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮清理还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1

# ⚠️ 必须 export：下面是 heredoc 内联的 Python，没法像 factor-recompute 那样走
# 命令行参数，唯一能带过去的通道就是进程环境。原来 run-metric 那份脚本只把
# 变量打进「开始」日志而 Python 里硬编码 ⇒ 一个只影响日志的配置变量比没有更坏：
# 看日志的人以为窗口改了，据此判断「表怎么没小下去」。
export KEEP_DAYS

{
  echo "===== $(date -u +%FT%TZ) 开始（保留 $KEEP_DAYS 天 env=$_ENV_LOADED）====="
} >> "$LOG"

# ⚠️ store 层不 commit（事务边界归调用方，见 cpt/storage/__init__.py）——
#    这里必须自己 commit，漏了就是静默回滚、删了跟没删一样。
timeout 10m .venv/bin/python - <<'PY' >> "$LOG" 2>&1
import os
import sys

sys.path.insert(0, ".")

# 解析失败要**报错退出**而不是静默用默认值：cron 无人盯着，
# 静默回落到 7 天只会让窗口慢慢漂回去，且日志里看不出任何异常。
keep = int(os.environ.get("KEEP_DAYS", "7"))
if keep < 1:
    raise SystemExit(f"保留天数必须是正整数（{keep}）")

import psycopg

from cpt.adapters._dbconfig import connection_kwargs
from cpt.storage.dashboard_run_store import prune

conn = psycopg.connect(**connection_kwargs())
try:
    # prune 失败会抛（「失败」与「没有过期行」不能同码），异常冒到下面。
    n = prune(conn, keep_days=keep)
    conn.commit()  # ⚠️ store 层不 commit，漏了就是静默回滚
    print(f"已删除 {n} 行（COALESCE(generated_at, created_at) 早于 {keep} 天前）")
    if n == 0:
        print("  （0 行 = 本来就没有过期数据，不是失败）")
finally:
    conn.close()
PY
rc=$?

{
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  # 与 run-metric / factor-recompute 同一个教训：「跑完了」和「跑挂了」不能同码。
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! 清理未完成（rc=$rc）—— dashboard 运行行会继续堆积 !!!!!"
  fi
} >> "$LOG"
exit $rc