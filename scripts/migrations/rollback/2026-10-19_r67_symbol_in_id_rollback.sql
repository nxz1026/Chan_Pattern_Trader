-- 回滚 `2026-10-19_r67_symbol_in_id.sql`。
--
-- ## 回滚到什么程度
--
-- 本迁移做了四件事，回滚按**逆序**撤掉前三件；**第四件（唯一索引）也撤**，
-- 因为「回滚」的语义是回到迁移前的状态，而不是留下一个迁移后的约束 ——
-- 留着它会让去重后的数据与旧代码（不处理冲突）组合出新的失败模式。
--
--   1. 加唯一索引 `uq_cpt_structure_event_sid_rev`  → DROP INDEX
--   2. 去重（删掉 417 组重复里的重复行）            → 从 `_dropped_20261019` 插回
--   3. 删除 cn 4 段行 + 84 行测试行                 → 从 `_dropped_20261019` 插回
--   4. 回填 crypto 的 id（加 `BTCUSDT:` 段）        → 照 `_id_backup_20261019` 反向改
--
-- ## 顺序为什么必须是这个
--
-- 1. 先撤唯一索引 —— 不撤，第 3 步插回重复行会直接撞唯一约束。
-- 2. **先插回去重删掉的行（它们现在是 5 段），再反向改 crypto 的 id 段。**
--    这样反向 UPDATE 会**顺带**把刚插回来的行也改回 4 段 —— 正是它们
--    迁移前的样子。若反过来（先改 id、再插回），插回来的就是 5 段行，
--    于是回滚后表里混着 4 段与 5 段 crypto 行，与迁移前的状态不等。
-- 3. 最后插回 cn 与测试行（4 段 + 5 段，原样）。
--
-- ## 前提
--
-- 三张备份表必须还在：
--   - `public.cpt_structure_event_id_backup_20261019`（crypto 的 id 映射）
--   - `public.cpt_structure_event_dropped_20261019`（cn + 测试行的整行副本）
--   - `public.cpt_structure_event_deduped_20261019`（去重删掉的重复行）
-- 备份表**不删**：回滚本身也可能要再撤（例如回滚后发现还有别的漏网之鱼），
-- 留着成本只有几十 MB，而丢了就再也拿不回来。确认无事后再手工 DROP。

BEGIN;

-- --------------------------------------------------------------------------- #
-- 0. 前置检查：备份表在不在
-- --------------------------------------------------------------------------- #
DO $$
DECLARE
    n_map   integer;
    n_rows  integer;
    n_dedup integer;
BEGIN
    IF to_regclass('public.cpt_structure_event_id_backup_20261019') IS NULL THEN
        RAISE EXCEPTION '缺少备份表 cpt_structure_event_id_backup_20261019，无法回滚';
    END IF;
    IF to_regclass('public.cpt_structure_event_dropped_20261019') IS NULL THEN
        RAISE EXCEPTION '缺少备份表 cpt_structure_event_dropped_20261019，无法回滚';
    END IF;
    IF to_regclass('public.cpt_structure_event_deduped_20261019') IS NULL THEN
        RAISE EXCEPTION '缺少备份表 cpt_structure_event_deduped_20261019，无法回滚';
    END IF;
    SELECT count(*) INTO n_map   FROM public.cpt_structure_event_id_backup_20261019;
    SELECT count(*) INTO n_rows  FROM public.cpt_structure_event_dropped_20261019;
    SELECT count(*) INTO n_dedup FROM public.cpt_structure_event_deduped_20261019;
    RAISE NOTICE '备份就位：id 映射 % 条，被删行 % 条，去重行 % 条', n_map, n_rows, n_dedup;
END $$;

-- --------------------------------------------------------------------------- #
-- 1. 撤唯一索引（必须先撤：插回重复行会撞上它）
-- --------------------------------------------------------------------------- #
DROP INDEX IF EXISTS public.uq_cpt_structure_event_sid_rev;

-- --------------------------------------------------------------------------- #
-- 2. 先插回去重删掉的行（此刻仍是 5 段，下一屏的反向 UPDATE 会把它们改回 4 段）
-- --------------------------------------------------------------------------- #
INSERT INTO public.cpt_structure_event
    (id, structure_id, event_type, status, revision, payload, occurred_at, created_at)
SELECT d.id, d.structure_id, d.event_type, d.status, d.revision,
       d.payload, d.occurred_at, d.created_at
FROM public.cpt_structure_event_deduped_20261019 d
ON CONFLICT (id) DO NOTHING;

-- --------------------------------------------------------------------------- #
-- 3. 把 crypto 的 id 与 payload 改回旧格式（逐行，含刚插回的重复行）
-- --------------------------------------------------------------------------- #
UPDATE public.cpt_structure_event e
SET structure_id = b.old_id,
    payload      = b.old_payload
FROM public.cpt_structure_event_id_backup_20261019 b
WHERE e.id = b.id;

-- --------------------------------------------------------------------------- #
-- 4. 插回被删除的 cn 行与测试行（id 一并还原，保持 append-only 的顺序语义）
-- --------------------------------------------------------------------------- #
INSERT INTO public.cpt_structure_event
    (id, structure_id, event_type, status, revision, payload, occurred_at, created_at)
SELECT d.id, d.structure_id, d.event_type, d.status, d.revision,
       d.payload, d.occurred_at, d.created_at
FROM public.cpt_structure_event_dropped_20261019 d
ON CONFLICT (id) DO NOTHING;

-- 插回 id 之后，bigserial 序列必须推到最大 id 之后 —— 否则下一条真实写入会撞主键。
-- `setval(..., max(id))` 而不是 `max(id)+1`：第三参数 true 表示「下一个 nextval
-- 返回 max+1」。若表为空则保持原值。
SELECT setval(
    'public.cpt_structure_event_id_seq',
    COALESCE((SELECT max(id) FROM public.cpt_structure_event), 1),
    true
);

-- --------------------------------------------------------------------------- #
-- 5. 终检：行数应回到迁移前水平，且 4 段行重新出现
-- --------------------------------------------------------------------------- #
DO $$
DECLARE
    n_total  integer;
    n_four   integer;
    n_five   integer;
BEGIN
    SELECT count(*) INTO n_total FROM public.cpt_structure_event;
    SELECT count(*) INTO n_four  FROM public.cpt_structure_event
     WHERE array_length(string_to_array(structure_id, ':'), 1) = 4;
    SELECT count(*) INTO n_five  FROM public.cpt_structure_event
     WHERE array_length(string_to_array(structure_id, ':'), 1) = 5;

    -- 迁移前是 20205 行左右（会随后续生产写入增长），所以只断言「4 段行回来了」
    IF n_four = 0 THEN
        RAISE EXCEPTION '回滚后没有任何 4 段行 —— 备份没插回去？';
    END IF;

    RAISE NOTICE '回滚完成：总 % 行，其中 4 段 % 行、5 段 % 行（5 段应为 84 行测试数据）',
        n_total, n_four, n_five;
END $$;

COMMIT;

-- --------------------------------------------------------------------------- #
-- 回滚后（人工）验收
-- --------------------------------------------------------------------------- #
-- 1) 段数分布应重新变成「4 段 + 那 84 行测试 5 段」：
--      SELECT array_length(string_to_array(structure_id,':'),1), count(*)
--        FROM public.cpt_structure_event GROUP BY 1;
-- 2) 唯一索引应已消失：
--      SELECT 1 FROM pg_class WHERE relname = 'uq_cpt_structure_event_sid_rev';
-- 3) 备份表仍在（确认无事后再手工 DROP）：
--      SELECT count(*) FROM public.cpt_structure_event_id_backup_20261019;
--      SELECT count(*) FROM public.cpt_structure_event_dropped_20261019;
