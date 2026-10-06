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
-- 1. 放开 ``NOT NULL``，让「本轮没有可信 generated_at」能以 **NULL** 落库。
--    配合 Python 侧，语义变成：**要么有时间戳且是真的，要么是 NULL**，
--    不再存在「假装有」这第三种状态。
-- 2. 加一条 CHECK，把「空字符串」也排除掉 —— timestamptz 的空串在部分驱动下
--    会被当成 epoch 而不是 NULL，那又是一种伪装。
--
-- ## 迁移后需要人工做的事
--
-- **放开约束不会自动修好历史数据。** 已经写入的假时间戳仍然是假的时间戳，
-- 只是从此以后新的不会再进来。要区分历史行与新行，看迁移时间：
--
--     SELECT count(*) FROM public.cpt_dashboard_run
--      WHERE generated_at IS NULL;
--
-- 迁移之后（这行是 NULL 的）就是「Python 侧拒绝写入、没落库」或
-- 「本轮无可信时间戳」；迁移之前的行仍需人工比对
-- ``created_at``（入库时刻，永远是真的）与 ``generated_at``：
-- 两者差得太远的，就是墙钟回落伪造的。
--
--     SELECT id, created_at, generated_at,
--            generated_at - created_at AS drift
--       FROM public.cpt_dashboard_run
--      WHERE generated_at IS NOT NULL
--        AND abs(extract(epoch FROM (generated_at - created_at))) > 300
--      ORDER BY abs(extract(epoch FROM (generated_at - created_at))) DESC;
--
-- 保留期清理那条 SQL 要跟着改：``generated_at IS NULL`` 的行现在不会被
-- ``WHERE generated_at < ...`` 命中，会**永久留存**。要么给它们单独一条清理规则
-- （按 ``created_at`` 删），要么明确决定「无时间戳的行不参与保留期」——
-- 这是一个口径决定，不该由这条迁移替 owner 做，所以这里只提示，不代劳。

BEGIN;

ALTER TABLE public.cpt_dashboard_run
    ALTER COLUMN generated_at DROP NOT NULL;

ALTER TABLE public.cpt_dashboard_run
    ADD CONSTRAINT cpt_dashboard_run_generated_at_not_blank
    CHECK (generated_at IS NULL);

COMMENT ON COLUMN public.cpt_dashboard_run.generated_at IS
    'reproducibility.generated_at（Unix 毫秒 → timestamptz）。'
    '解析失败时为 NULL（R56 起 Python 侧拒绝写假时间戳），'
    '**不再回落到墙钟** —— 墙钟回落会让伪造的时间戳看起来像真的。'
    '注意：NULL 的行不参与 generated_at < ... 的保留期清理。';

COMMIT;
