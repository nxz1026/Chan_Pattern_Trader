-- R56（2026-10-06）把 ``public.cpt_run_metric`` 的 schema 固化成迁移文件。
--
-- ## 为什么现在才补
--
-- 这张表**一直有 DDL**，但它只以 ``cpt/storage/run_metric_store.py::_DDL``
-- 那个字符串的形式存在。R45 复盘时记过一个裁决：「先不转成迁移文件」，
-- 理由是它 ``CREATE TABLE IF NOT EXISTS``、且当时与真库零漂移。
--
-- 那个裁决有一个**致命前提**：``IF NOT EXISTS`` 意味着**表已存在时 DDL 整个不生效**。
-- 于是改 ``_DDL`` 的列之后：
--
--   - 任何**测试都不会变红**（表在库里已存在，改的是字符串常量）；
--   - 已存在的表**不会**被改（这正是 ``IF NOT EXISTS`` 的语义）。
--
-- ⇒ 代码与库可以**无声漂移**，而且没有任何检查会报警。这与本仓反复处理的
-- 「数字看起来都很合理，没有一处会让人怀疑」是同一类病。
--
-- **本文件不改变 schema**，只是把既有定义**逐字搬进 SQL**，
-- 让「这张表长什么样」不再只有 Python 字符串这一个来源。往后要改列：
-- **先改这里**（``ALTER TABLE``），再改 ``_DDL``，两条路径都要动。
--
-- ## 形状：三轨并存
--
-- 一行 = 「某一轮观测到的某个标的」的水位 + 指纹 + 结构计数。
--
--   第一轨 水位   last_bar_time / gap_count / stale / factor_coverage /
--                  snapshot_age_ms —— 「数据还新不新」
--   第二轨 指纹   config_hash / dataset_hash / rules_version / backend ——
--                  「同样输入能不能复现」
--   第二轨 计数   bar_count / fractal_count / bi_count / zhongshu_count /
--                  trend_type_count —— 「结构长什么样」
--   结论          health（ok/degraded/failing 三态）+ detail（jsonb 原文）
--
-- ⚠️ ``health`` 是**三态**不是布尔：``degraded`` 是「配置导致的已知降级」，
-- ``failing`` 才是要叫人起床的。把两者并成一个布尔会让看板天天误报。
--
-- ⚠️ **无 CHECK 约束**（``kind`` / ``health`` 都没加 ``CHECK ... IN (...)``）：
-- 这是**照抄** ``_DDL`` 的既有形状，不是本文件的疏漏。代码侧用
-- ``KIND_RUN`` / ``KIND_INSPECTION`` 与 ``HEALTH_VALUES`` 收口，
-- 加 DB 约束是**另一次**改动，需先确认线上存量行全部合规（改约束要扫全表，
-- 与 R39 砍索引是同一条纪律：先量再动）。
--
-- ## 索引为什么是这三个
--
-- 三个索引各自对应一条**真实在跑的**查询，不是预防性添加：
--
--   (observed_at DESC)              巡检看最近状态 / prune 扫窗口
--   (kind, observed_at DESC)        按类分窗清理（run 与 inspection 窗口不同）
--   (market, symbol, observed_at DESC) 单标的的水位趋势
--
-- ⚠️ **这张表会无界增长**：run 行是「每轮一行」，而
-- ``run_metric_store.prune`` 长期**零调用方**（R45 实测：含测试 grep 无引用）。
-- 每日清理由 ``deploy/cron/run-metric-prune-daily.sh`` 承担。
--
-- ⚠️ 表在 ``emotion_core`` 库，不在本仓 ``public`` 之外的任何 schema。

BEGIN;

CREATE TABLE IF NOT EXISTS public.cpt_run_metric (
    id                bigserial   PRIMARY KEY,
    observed_at       timestamptz NOT NULL DEFAULT now(),
    kind              text        NOT NULL DEFAULT 'run',
    market            text        NOT NULL DEFAULT '',
    symbol            text        NOT NULL DEFAULT '',
    config_hash       text        NOT NULL DEFAULT '',
    dataset_hash      text        NOT NULL DEFAULT '',
    rules_version     text        NOT NULL DEFAULT '',
    backend           text        NOT NULL DEFAULT '',
    bar_count         integer     NOT NULL DEFAULT 0,
    fractal_count     integer     NOT NULL DEFAULT 0,
    bi_count          integer     NOT NULL DEFAULT 0,
    zhongshu_count    integer     NOT NULL DEFAULT 0,
    trend_type_count  integer     NOT NULL DEFAULT 0,
    last_bar_time     bigint      NOT NULL DEFAULT 0,
    gap_count         integer     NOT NULL DEFAULT 0,
    stale             boolean     NOT NULL DEFAULT false,
    factor_coverage   numeric     NOT NULL DEFAULT 0,
    snapshot_age_ms   bigint      NOT NULL DEFAULT 0,
    health            text        NOT NULL DEFAULT 'ok',
    detail            jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- 巡检看最近状态 / prune 扫保留窗口
CREATE INDEX IF NOT EXISTS cpt_run_metric_observed_idx
    ON public.cpt_run_metric (observed_at DESC);

-- 按类分窗清理：run 行与 inspection 行窗口不同（见 cron 里的 prune）
CREATE INDEX IF NOT EXISTS cpt_run_metric_kind_idx
    ON public.cpt_run_metric (kind, observed_at DESC);

-- 单标的的水位趋势（某只票最近 N 轮长什么样）
CREATE INDEX IF NOT EXISTS cpt_run_metric_symbol_idx
    ON public.cpt_run_metric (market, symbol, observed_at DESC);

COMMENT ON TABLE public.cpt_run_metric IS
    'CPT 每轮观测的水位/指纹/结构计数（append-only）。巡检与看板读它。'
    'schema 双来源：此处 + cpt/storage/run_metric_store.py::_DDL，改列须两处同改。';
COMMENT ON COLUMN public.cpt_run_metric.kind IS
    'run（每轮一行，无界增长，靠 cron prune）/ inspection（每日状态比对，窗口短）。';
COMMENT ON COLUMN public.cpt_run_metric.health IS
    '三态 ok/degraded/failing。degraded 是「配置导致的已知降级」，不是故障 —— 并成布尔会让看板误报。';
COMMENT ON COLUMN public.cpt_run_metric.detail IS
    '人类可读的明细（jsonb），留原文以便事后不用回放日志。';

COMMIT;
