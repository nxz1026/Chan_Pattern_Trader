-- R56：放开 cpt_dashboard_run.generated_at 的 NOT NULL，让「假时间戳」可见
--
-- ## 背景
--
-- R56 之前，``cpt/storage/dashboard_run_store.py`` 在 ``reproducibility.generated_at``
-- 解析失败时**静默回落到墙钟**（只记 warning）。后果是索引里会积累一批
-- **看起来完全正常、实际是编出来的**时间戳 —— 它们参与
-- ``ORDER BY generated_at DESC``、参与 ``DELETE WHERE generated_at < ...`` 的保留期
-- 清理，看起来一切正常，没有���何办法从表里分辨哪一行是真的。
--
-- R56 已把 Python 侧改成「解析不了就**拒绝写入** + ERROR 日志」。但那只解决了
-- **不再产生**假时间戳，解决不了**已经存在的**那些行：它们还在表里，还是分不出来。
--
-- ## 这次改什么
--
-- 放开 ``NOT NULL``，让「本轮没有可信 generated_at」能以 **NULL** 落库。
-- 配合 Python 侧，语义变成：**要么有时间戳且是真的，要么是 NULL**，
-- 不再存在「假装有」这第三种状态。
--
-- ⚠️ **不要**再加 ``CHECK (generated_at IS NULL)`` 这种约束 —— 那是「必须永远是
-- NULL」，现存 46 行全部不满足，DDL 会直接失败（第一版就踩了，整条事务回滚）。
-- 本来想加的「排除空串」也是多余的：timestamptz 本身就把 ``''`` 当非法输入拒掉，
-- 不存在「空串被当成 epoch」这条路径。
--
-- 已于 2026-10-06 在生产执行并冒烟验证：is_nullable NO→YES，写入 NULL 成功，
-- 清理后无残留，46 行数据未受影响。
--
-- ## 迁移后需要人工做的事
--
-- **放开约束不会自动修好历史数据。** 已经写入的假时间戳仍然是假的时间戳，
-- 只是从此以后新的不会再进来。
--
-- ⚠️ 本表**没有 created_at 列**（真实 schema 只有 run_id / dataset_hash /
-- generated_at / body_recorded / snapshot 五列），所以**不能**用「与 created_at
-- 的漂移量」去筛 —— 早先的草稿写错了，此处更正。
--
-- 正确的识别方式：看 ``snapshot`` 这个 jsonb 里**有没有**原始的
-- ``reproducibility.generated_at``。R56 之前的实现是在解析失败时把墙钟写进
-- ``generated_at`` 列，而 snapshot 内部那个字段仍然是缺的 —— 所以
-- **snapshot 里缺这个键的行，其列值就是伪造的**：
--
--     -- 已核实：46 行里 29 行有该键（列值与 snapshot 一致），
--     --         17 行没有 ⇒ 这 17 行的 generated_at 是墙钟回落伪造的
--     SELECT run_id, generated_at
--       FROM public.cpt_dashboard_run
--      WHERE snapshot->'reproducibility'->>'generated_at' IS NULL
--      ORDER BY generated_at DESC;
--
-- 要不要清理这 17 行、以及清理后是否需要重跑那些 run，是口径决定，
-- 本迁移不代做。
--
-- ## 另一件需要人工决定的事
--
-- 保留期清理那条 SQL（``DELETE ... WHERE generated_at < now() - interval '7 days'``）
-- 要跟着看：``generated_at IS NULL`` 的行**不会被它命中**，会永久留存。
-- 要么给它们单独一条按其它键清理的规则，要么明确决定「无时间戳的行不参与
-- 保留期」—— 这不该由这条迁移替 owner 做，所以这里只提示。

BEGIN;

ALTER TABLE public.cpt_dashboard_run
    ALTER COLUMN generated_at DROP NOT NULL;

COMMENT ON COLUMN public.cpt_dashboard_run.generated_at IS
    'reproducibility.generated_at（Unix 毫秒 → timestamptz）。'
    '解析失败时为 NULL（R56 起 Python 侧拒绝写假时间戳），'
    '**不再回落到墙钟** —— 墙钟回落会让伪造的时间戳看起来像真的。'
    '注意：NULL 的行不参与 generated_at < ... 的保留期清理。'
    '中文说明见 scripts/migrations/2026-10-06_r56_dashboard_run_generated_at_nullable.sql。';

COMMIT;
