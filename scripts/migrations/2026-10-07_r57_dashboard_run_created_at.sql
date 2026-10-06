-- R57：给 cpt_dashboard_run 补入库时间戳，并把保留期清理改成能覆盖 NULL 行
--
-- ## 为什么
--
-- R56 放开了 ``generated_at`` 的 NOT NULL，让「本轮没有可信时间戳」能以 NULL
-- 落库。但**保留期清理那条 SQL 命中不了 NULL 行**：
--
--     DELETE FROM public.cpt_dashboard_run
--      WHERE generated_at < now() - interval '7 days';   -- NULL 永远不满足
--
-- 根因不是忘了写 ``OR generated_at IS NULL``，而是**这张表压根没有第二个时间戳**
-- —— 真实 schema 只有 run_id / dataset_hash / generated_at / body_recorded /
-- snapshot 五列，**没有 created_at**。也就是说 NULL 行**没有任何字段能让它变老**，
-- 只能永久留存。
--
-- ## 这次改什么
--
-- 1) 加 ``created_at``（入库时刻，``DEFAULT now()``，NOT NULL）——
--    补上「这行是什么时候进来的」这个**永远可信**的锚点。它与 ``generated_at``
--    的区别很重要：``generated_at`` 是**业务产物声称的**时间（可能被伪造），
--    ``created_at`` 是**数据库记录的**入库时刻（不可能被伪造）。
--
-- 2) 保留期清理改成一条表达式，两类行都按 7 天过期：
--
--        WHERE COALESCE(generated_at, created_at) < now() - interval '7 days'
--
--    正常行走 ``generated_at``（沿用原语义），NULL 行走 ``created_at``。
--
-- ## 顺带纠正一处此前写错的检测法
--
-- 本仓库 2026-10-06 的迁移 ``..._r56_..._nullable.sql`` 里写过「用 ``generated_at``
-- 与 ``created_at`` 的漂移量来筛历史伪造行」——**那时这张表还没有 created_at**，
-- 那段 SQL 跑不起来。R56 已改用正确方法（看 ``snapshot`` jsonb 里有没有原始的
-- ``reproducibility.generated_at``，缺的就是伪造的），并据此在生产上识别出
-- **17 行**伪造数据，已在 R57 里把它们改标为 NULL（内容保留，未删除）。
--
-- ## 注意：created_at 的回填值不是真实入库时刻
--
-- ``ADD COLUMN ... DEFAULT now()`` 会把**迁移执行时刻**填给所有存量行，包括那
-- 17 行伪造成 2026-09/10 的。它们本来就没有可信的入库时间，``created_at = now()``
-- 是**可接受的近似**（误差 = 迁移时刻 - 真实入库时刻）。后果：这 17 行会在
-- **迁移后 7 天**被保留期清理掉 —— 对 3 个月前的陈旧垃圾行来说这正是想要的，
-- 但要知道它不是「按真实年龄」而「按迁移时刻起算」。

BEGIN;

ALTER TABLE public.cpt_dashboard_run
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

COMMENT ON COLUMN public.cpt_dashboard_run.created_at IS
    '入库时刻（数据库记录，不可能被伪造）。'
    '与 generated_at 的区别：generated_at 是**业务产物声称的**时间，'
    'R56 起解析失败即为 NULL（宁可不写也不写假的）；created_at 是**入库时刻**，永远可信。'
    '保留期清理用 COALESCE(generated_at, created_at)，让 NULL 行也能过期。'
    '注意：2026-10-07 加列时存量行被回填为迁移时刻，不是真实入库时刻。';

-- 索引：保留期清理会按 COALESCE(...) 过滤
CREATE INDEX IF NOT EXISTS idx_cpt_dashboard_run_coalesce_time
    ON public.cpt_dashboard_run ((COALESCE(generated_at, created_at)));

COMMIT;
