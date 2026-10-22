-- R90（2026-10-22）新建 CPT 盘中封板快照表 ``public.intraday_seal_snapshot``。
--
-- ## 为什么新建
--
-- R89 的采集器（``scripts/intraday_collectd.py``，每 2 分钟一轮）只把汇总写进
-- ``~/logs/intraday-collect.log``，**没有股票代码**。emotion-core 要消费时只能
-- 自己调 CPT 的 ``SinaQuoteClient.fetch_quotes()`` 再自己判涨停 —— 判据分叉，
-- 违反 ADR 0003 §E2「原始采集归 CPT、触发判据归 emotion-core」。
--
-- 本表存的是**CPT 判定完的结论**（``seal_strength()`` 的输出），不是原始五档：
-- 阈值只有一个实现，跨仓不会漂。
--
-- ## 为什么只有 5 列
--
-- 每一列都是采集端**已经算出来**的，没有一列是为了「方便查询」而冗余：
--
-- - ``at`` / ``code``：主键，决定「哪一轮的哪只票」。
-- - ``sealed``：这一轮是否封在板上（``seal_strength()["at_limit"]``）。
--   判不了的情况（停牌、缺档位）**不写行**，而不是写 ``false`` ——
--   「没采到」与「没封板」必须是两件事。
-- - ``bid1_lots``：封单量（手）。封板强度只有它和 ``limit_price`` 能算，
--   存汇总数会让下游无法做「封单 < 3 万手」这种阈值判断。
-- - ``limit_price``：这一轮用的涨停价。**存下来是为了对账** —— 口径改了
--   （如 920 号段从 10% 改 30%）时历史行能自证是哪个口径算的。
--
-- 不存 open/high/low、不存涨跌幅：那些是``daily_bar`` 与采集日志的职责，
-- 抄进本表只会多一处会分叉的真相。
--
-- ## 为什么 append-only
--
-- 每轮每票一行，同一 ``(at, code)`` 不覆盖。封板存活（几点开板、开了几次）
-- 只能从时间序列读出来，``UPDATE`` 会抹掉。
--
-- ## 保留期
--
-- 交易时段 120 轮/交易日 × 40 只 ≈ 4,800 行/日 ≈ 120 万行/年。暂不设自动清理
-- （与 ``cpt_signal_event`` 同口径）；等确实变大再加 ``deploy/cron`` 的 prune。

BEGIN;

CREATE TABLE IF NOT EXISTS public.intraday_seal_snapshot (
    at          timestamptz     NOT NULL,          -- 这一轮的采集时刻（Asia/Shanghai 换算成 timestamptz）
    code        text            NOT NULL,          -- 6 位纯代码，与 SinaQuoteClient.fetch_quotes 的键同形
    sealed      boolean         NOT NULL,          -- 是否封在板上（seal_strength()["at_limit"]）
    bid1_lots   double precision,                   -- 封单量（手）；判不了时为 NULL
    limit_price double precision,                   -- 本轮使用的涨停价；判不了时为 NULL
    PRIMARY KEY (at, code)
);

-- 按票回看某只票当天的封板轨迹（「几点开板、开了几次」）
CREATE INDEX IF NOT EXISTS idx_intraday_seal_snapshot_code_at
    ON public.intraday_seal_snapshot (code, at);

-- 只保留最近 N 天的查询用
CREATE INDEX IF NOT EXISTS idx_intraday_seal_snapshot_at
    ON public.intraday_seal_snapshot (at DESC);

COMMENT ON TABLE public.intraday_seal_snapshot IS
    'CPT 盘中封板快照（append-only，每 2 分钟一轮）。存 CPT 判定结论，不存原始五档；'
    '判不了的轮次不写行。「没采到」与「没封板」靠「有没有行」区分。';

COMMENT ON COLUMN public.intraday_seal_snapshot.limit_price IS
    '本轮使用的涨停价 —— 存下来自证口径（如 920 号段 30%），口径变更后历史行仍可对账。';

COMMIT;
