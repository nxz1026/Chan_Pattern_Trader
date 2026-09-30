-- R20（2026-09-30）asel.ref_adjust_factor 补列。
--
-- ## 为什么补
--
-- `cpt/adapters/a_share_factor.py::upsert_factor_rows` 写的是 **9 列**
-- （code/trade_date/hfq_factor/source/source_url/source_ref/as_of/
-- available_at/fetched_at），而线上表当时**只有 3 列**（code/trade_date/
-- hfq_factor），所以按需取因子这条生产路径
-- （`cpt/application/a_share_snapshot.py::_persist_ondemand_factors`，
--  `CPT_ASHARE_ONDEMAND_FACTOR=1` 时走）**必然报错**。
--
-- 列不是新设计的，是**曾经存在过、随表重建丢了**：
--   · `docs/progress-log.md:1402` 记「`asel.ref_adjust_factor.source` 7,200 行迁移 ✅」；
--   · `docs/handoff-20260925-leftover-fixes.md:232` 有
--     `UPDATE ... SET source='tx:fqkline' WHERE source='tencent_fqkline'`。
-- 也就是说，当年的迁移确实建过 `source`，只是没有随表结构落进仓库。
--
-- ## 为什么写成文件
--
-- 本仓**没有任何 .sql 迁移文件**：`scripts/factor_backfill.py:15-16` 注释引用的
-- `migrations/0002_p0_reference.sql:96` 从来不存在（本轮 grep 全仓 0 命中）。
-- 没有可重放的迁移 → 表被重建时列会再次静默消失。补一个幂等文件，让下次
-- 重建有据可查。
--
-- ## 幂等与回填
--
-- 全部 `ADD COLUMN IF NOT EXISTS`，可重复执行。
-- 补列本身**不回填数据**：存量 3,388,417 行的 `hfq_factor` 全是列默认值
-- `1.0`（纯占位，不是不复权因子），由 `scripts/factor_backfill.py --mode full`
-- 按 `(code, trade_date)` upsert 覆盖，不要在这里手写 UPDATE。
-- 溯源列留 NULL 表示「补列前写入、来源不可考」，比编一个假 source 诚实。

BEGIN;

ALTER TABLE asel.ref_adjust_factor
    ADD COLUMN IF NOT EXISTS source       text,
    ADD COLUMN IF NOT EXISTS source_url   text,
    ADD COLUMN IF NOT EXISTS source_ref   text,
    ADD COLUMN IF NOT EXISTS as_of        timestamptz,
    ADD COLUMN IF NOT EXISTS available_at timestamptz,
    ADD COLUMN IF NOT EXISTS fetched_at   timestamptz;

COMMENT ON COLUMN asel.ref_adjust_factor.source IS
    '取数来源；tx:fqkline = 腾讯 web.ifzq.gtimg.cn fqkline。NULL = 补列前写入、来源不可考。';
COMMENT ON COLUMN asel.ref_adjust_factor.source_url IS
    '取数端点 URL（不含具体 param）。';
COMMENT ON COLUMN asel.ref_adjust_factor.source_ref IS
    '单行溯源串：web.ifzq.gtimg.cn fqkline day/hfq {trade_date}，由 factor_source_ref() 构造。';
COMMENT ON COLUMN asel.ref_adjust_factor.hfq_factor IS
    '后复权因子 = hfq_close / raw_close（同源同对）。1.0 是列默认值占位，不代表不复权。';

COMMIT;
