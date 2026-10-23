#!/usr/bin/env bash
# 因子**沿用**：把暂存表每票最新一档的因子沿用到 daily_bar 的最新交易日，并 promote。
#
# ## 为什么需要它（2026-10-23）
#
# 全市场重算在 **02:20 UTC（北京 10:20）** 跑，而当天 K 线要等
# ``emotion-core-daily``（北京 17:20 = 09:20 UTC，跑约 11.5 分钟）才入库
# ⇒ **重算的 target 永远停在前一天** ⇒ 当天因子全市场缺失
# ⇒ CPT 侧 ``price_ratio`` 取不出 ⇒ **A 股结构位整段退化为「不复权」**
# （实测 2026-10-23：生产表当天只有 1/5211 只，由按需拉取补上）。
#
# ## 为什么「沿用」是精确的，不是近似
#
# 因子是**公司行动的台阶函数**：无除权事件的日期，因子值与前一日逐位相同
# （实测 600519 的 6.39414671 连续多日不变，``source=eastmoney:events steps=13``）。
# ⇒ 沿用与重算的结果**逐位相同**（同一套事件源、同一套算法）。
# 有除权事件的当天由**全市场重算**纠正（沿用行的 source 标成 ``carry_forward:``，事后可查）。
#
# ## 两条安全阀
#
# 1. **只补「生产表当天没有」的票** —— 生产表当天已有的行来自按需拉取（腾讯比值），
#    那是实测更准的值；沿用绝不能覆盖它（否则 promote 会把对的换成错的）。
# 2. **写暂存表，不直接写生产表** —— 搬运一律走既有的 promote_recompute_factors()。
#
# ## 装法
#
# crontab 直接指向仓内路径（本文件与 factor-recompute-daily.sh 同款约定）：
#     45 9 * * * /home/ubuntu/DSH/Chan_Pattern_Trader/deploy/cron/factor-carry-forward.sh
set -uo pipefail

REPO="${CPT_REPO:-/home/ubuntu/DSH/Chan_Pattern_Trader}"
LOG=/home/ubuntu/logs/factor-carry-forward.log
LOCK=/home/ubuntu/logs/factor-carry-forward.lock

mkdir -p /home/ubuntu/logs
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] 上一轮还在跑，跳过" >> "$LOG"
  exit 0
fi

cd "$REPO" || exit 1
{
  echo "===== $(date -u +%FT%TZ) 开始 ====="
  .venv/bin/python scripts/factor_carry_forward.py "$@" 2>&1
  rc=$?
  echo "===== $(date -u +%FT%TZ) 结束 rc=$rc ====="
  # ⚠️ rc≠0 要能被外部看见：沿用失败 ⇒ 当天因子缺失 ⇒ 结构位退化为不复权。
  # 与 factor-recompute-daily.sh 同款约定：3 = 未完成。
  if [ "$rc" -ne 0 ]; then
    echo "!!!!! 本轮未完成（rc=$rc）—— 当天因子可能缺失，结构位会退化为不复权 !!!!!"
  fi
  exit $rc
} >> "$LOG" 2>&1
