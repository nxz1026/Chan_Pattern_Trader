-- R59（2026-10-10）给 ``public.cpt_signal_event(transition_time)`` 加索引。
--
-- ## 背景（审计 L12）
--
-- ``load_trade_decisions``（``cpt/storage/signal_event_store.py``）是交易机的
-- **日切查询**，2026-10-07 新增：
--
--   SELECT ... FROM public.cpt_signal_event
--    WHERE transition_time >= %s::date
--      AND transition_time <  (%s::date + interval '1 day')
--      AND status = ANY(%s)
--    ORDER BY transition_time DESC, id DESC
--    LIMIT %s;
--
-- 它**不带 signal_id / code**，而 r21 只建了
-- ``(signal_id, id DESC)`` / ``(code, id DESC)`` 两个索引 —— 这两个都用不上，
-- 于是这个查询每天对全表做 seq scan + sort，随事件累积线性变慢。
--
-- ## 本迁移做什么
--
-- 建 ``(transition_time DESC, id DESC)`` 复合索引：前导列直接服务
-- ``transition_time`` 的 range 条件，第二列 ``id DESC`` 与 ``ORDER BY
-- transition_time DESC, id DESC`` 对齐，让排序也能走索引。
--
-- 刻意**不**建 ``(status, transition_time)``：``status`` 是高基序列里的低基数
-- 列（5 个取值），把它放前导列会让 range 扫描退化成对每个 status 各扫一遍；
-- ``status = ANY(...)`` 的过滤放回索引条件过滤更划算。这是审计 L12 的保守修法 ——
-- 只加索引、不改查询、不加约束。
--
-- ## 迁移风格 / 复跑
--
-- ``CREATE INDEX IF NOT EXISTS`` ⇒ 可重复跑（第二次是 no-op）。索引**非**
-- CONCURRENTLY：本仓迁移都是手工 psql 执行、表当前规模很小，封锁代价可忽略。
--
-- ## 核对（跑完可选执行）
--
--   SELECT indexname FROM pg_indexes
--    WHERE tablename = 'cpt_signal_event'
--      AND indexname = 'idx_cpt_signal_event_transition_time';
--
-- 期望：1 行。

BEGIN;

-- 交易机日切查询：transition_time 区间过滤 + (transition_time DESC, id DESC) 排序
CREATE INDEX IF NOT EXISTS idx_cpt_signal_event_transition_time
    ON public.cpt_signal_event (transition_time DESC, id DESC);

COMMIT;
