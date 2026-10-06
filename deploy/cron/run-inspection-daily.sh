#!/usr/bin/env bash
# 每日巡检：读 public.cpt_run_metric + 数据源状态 → 状态变化或出现 failing 时发飞书。
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
ENV_FILE=$REPO/deploy/env/cpt-dashboard.env
LOG=/home/ubuntu/logs/run-inspection.log
LOCK=/home/ubuntu/logs/run-inspection.lock

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
# 与另两个脚本对齐：巡检慢起来时（网络卡/DB 锁）不能让上一轮还占着就起第二轮，
# 两份巡检结论并发写飞书会变成互相矛盾的告警。
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮巡检还在跑，跳过" >> "$LOG"
  exit 0
fi

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
  # ⚠️⚠️ 这里必须**非零退出**（R56 修复）。原来只往 stderr 写一行就继续往下跑，
  # 而 scripts/run_inspection.py 在「一切正常且状态没变化」时返回 0 ——
  # 于是 **webhook 丢了 = 每天报「成功」= 永远不告警**，
  # 恰恰是 .env.example 警告的那一幕。告警通道自己坏了却报告成功，
  # 是最坏的一种失效：外部看起来一切正常。
  echo "[$(date -u +%FT%TZ)] 找不到 $ENV_FILE，飞书告警不可用" >&2
  echo "[$(date -u +%FT%TZ)] 找不到 $ENV_FILE，飞书告警不可用" >> "$LOG"
  exit 78   # EX_CONFIG：配置缺失，与「跑挂了」区分开
fi

# 再兜一层：文件在但**键是空的**同样等于没配（模板复制过来忘了填值最常见）。
: "${CPT_FEISHU_WEBHOOK:?CPT_FEISHU_WEBHOOK 未设置 —— 巡检将无法告警，退出}"
echo "[$(date -u +%FT%TZ)] 巡检开始" >> "$LOG"

# ⚠️ 必须有 timeout —— 另两个脚本都有，只有这个没有。没有超时的话，
# 一旦 DB/网络挂住，这一轮就永远挂着，而 cron 会在明天再起一个 ⇒ 并发叠加。
timeout 30m .venv/bin/python scripts/run_inspection.py
rc=$?

{
  echo "[$(date -u +%FT%TZ)] 巡检结束 rc=$rc"
  # ⚠️ R44 教训（factor-recompute 同款）：「跑完了」和「跑挂了」不能同码。
  # run_inspection.py 在「无异常且状态未变」时返回 0 —— 那是**正常的静默日**，
  # 外部看门狗必须能靠这一行把「静默」和「死过」区分开。
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! 本轮巡检未完成（rc=$rc）—— 可能没发出去告警 !!!!!"
  fi
} >> "$LOG"
exit $rc
