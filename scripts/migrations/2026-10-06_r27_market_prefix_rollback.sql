-- R27-4 回滚：把 `structure_id` 还原成不带市场前缀的旧格式。
--
-- 与 `2026-10-06_r27_market_prefix.sql` 配套。**仅在代码已回滚到 R26 版本后
-- 才可用** —— 代码里 `structure_id_of()` 强制带 market，跑着新代码却把库里
-- 改回旧格式，会造成与迁移前完全相同的「重复写 created」。
--
-- 本表无 FK，回滚代价为 0。

BEGIN;

DO $$
DECLARE
    restored integer;
BEGIN
    IF to_regclass('public.cpt_structure_event_id_backup_20261001') IS NULL THEN
        RAISE EXCEPTION
            '找不到备份表 public.cpt_structure_event_id_backup_20261001 —— 无法回滚';
    END IF;

    UPDATE public.cpt_structure_event e
    SET structure_id = b.structure_id
    FROM public.cpt_structure_event_id_backup_20261001 b
    WHERE e.structure_id = b.new_id;

    GET DIAGNOSTICS restored = ROW_COUNT;

    IF restored = 0 THEN
        RAISE EXCEPTION '没有任何行被还原 —— 备份表与当前 id 对不上？';
    END IF;

    RAISE NOTICE '还原 % 行到无前缀格式', restored;
END $$;

COMMIT;

-- 备份表确认不再需要时可手工删除（本仓不自动 DROP —— 留证据比省空间重要）：
--   DROP TABLE IF EXISTS public.cpt_structure_event_id_backup_20261001;
