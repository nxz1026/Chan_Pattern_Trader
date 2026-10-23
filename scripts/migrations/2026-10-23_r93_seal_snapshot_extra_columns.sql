-- R93（2026-10-23）：封板快照补 4 列 —— emotion-core N5 需求
--
-- 背景：emotion-core 要建常驻采集器（`emotion-intraday.service`），现有 5 列不够它判
-- 「封住之后有没有松」和「ST 板比 5%」。N5 清单要 4 列：price / prev_close /
-- chg_pct / name。
--
-- CPT 侧只接受 3 列 + 1 个布尔，理由见 docs/intraday-seal-contract.md §2：
--
--   price         要。seal_weaken 判「现价掉出涨停」必需。
--   prev_close    要。算涨停价基准 + 交叉校验 CPT 自算的 limit_price。
--   distance_pct  要，**不是 chg_pct**。(limit_price - price) / limit_price * 100，
--                 负数 = 已在板上。这是派生的派生，用 CPT 的 Decimal 取整口径算的，
--                 读方自己除不出来。而 chg_pct = (price-prev_close)/prev_close 三个
--                 成分都在，读方自己算 —— 存成列就是同一事实的第二份拷贝，
--                 两份迟早不一致（R76 踩过：median 算两遍导致口径打架）。
--   is_st         要，**不是 name**。name 会变（600xxx 改名为 ST某某），
--                 同一条 at 上的历史行会自相矛盾。读方要 name 只为判 ST，
--                 那就只存判定结果，判据留在 CPT（ADR 0003 §E2 只许一处）。
--
-- 存量 99 行这 4 列全部 NULL，**不回填** —— 它们当时确实没采，补出来的是
-- 推算值不是采集值。
--
-- 全部允许 NULL：判不了的轮次就是不知道，不许拿 0 冒充。

ALTER TABLE public.intraday_seal_snapshot
    ADD COLUMN IF NOT EXISTS price        double precision,
    ADD COLUMN IF NOT EXISTS prev_close   double precision,
    ADD COLUMN IF NOT EXISTS distance_pct double precision,
    ADD COLUMN IF NOT EXISTS is_st        boolean;

COMMENT ON COLUMN public.intraday_seal_snapshot.price IS
    '这一轮现价（采集时的 last）。判「封住之后有没有松」要用。判不了为 NULL。';
COMMENT ON COLUMN public.intraday_seal_snapshot.prev_close IS
    '昨收。算涨停价基准 + 交叉校验 limit_price。判不了为 NULL。';
COMMENT ON COLUMN public.intraday_seal_snapshot.distance_pct IS
    '(limit_price - price) / limit_price * 100。0 = 封在板上，负数 = 已在板上，正数 = 距板还差。NULL = 算不出。';
COMMENT ON COLUMN public.intraday_seal_snapshot.is_st IS
    'ST 判定结果（判据在 CPT intraday_rules.is_st_name）。NULL = 没判（缺 name）。';
