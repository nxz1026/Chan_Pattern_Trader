-- R27-4（2026-10-01）给 `structure_id` 加市场前缀。
--
-- ## 为什么这是破坏性变更
--
-- R26 建表时 id 是 `f"{kind}:{level}:{start_time}"`，**不含市场**，而本表也**不存
-- market 列**。两个市场只要在同 level 上撞上同一个 `start_time`，就会**静默合并**成
-- 同一个结构 —— 而且不会报任何错，因为 id「确实」同输入同输出，只是这个「同」跨了
-- 市场。届时 A 股的笔会继承加密笔的 revision，状态机从错误的前态继续推进。
--
-- 上线后实测：A 股 101 个 id 与加密 485 个 id 交集为 0，没出事。但那是日线
-- （`start_time` 恒在 UTC 0 点）与小时线（对齐整点）时间轴**恰好错开**，属运气不是
-- 设计。所以补前缀，并把 market 做成 `structure_id_of()` 的**必填**参数。
--
-- ## 与代码的部署顺序（不能错）
--
-- 本迁移**必须与 `cpt` 代码同一次部署**生效。中间任何时刻新旧格式并存都会导致
-- 同 id 匹配不上：每轮 diff 都把全部结构当新结构，重复写 `created`，revision 从 1
-- 重新开始。顺序是：停服 → 跑本迁移 → 起服。
--
-- ## 归属判定规则（可复现，不硬编码 id 列表）
--
-- 表里没有 market 列，所以历史 672 行必须**反推**归属。规则：
--
--   时间戳命中 `public.daily_bar` 的某个交易日（A 股 bar 恒在 UTC 0 点）
--     → 标 `cn:`
--   否则
--     → 标 `crypto:`
--
-- 判定前做过三道自校验（结论见 progress-log R27 §九）：
--
-- 1. 已确认的 101 个 a-share id **全部**命中日线表（0 漏）→ 规则可靠；
-- 2. 已确认的 483 个 crypto id 只有 **7** 个也命中日线表（1.4%）→ 规则保守；
-- 3. 唯一 4 个 A 股从不出现的时刻（16:00）的历史 id 全部落到 `crypto:`，与规则一致。
--
-- ## 7 行的已知代价（方向是安全的）
--
-- 那 7 行是**加密**结构恰好落在 A 股交易日的 UTC 0 点，会被误标成 `cn:`。后果：
-- 加密侧继续用 `crypto:` 写新事件，与这 7 行匹配不上 → 各记一次 `created`
-- （**重复**，不是合并），且这 7 个基础 id 在 A 股集合里不存在（实测交集为 0），
-- 所以 `cn:` 侧永远不会有东西来认领它们 → 它们成为孤儿行，不影响任何状态派生。
--
-- 方向说明：误标只会造成**重复**，不会造成**合并**。合并才是危险的那个方向
-- （状态机从错误前态推进）。所以宁可重复不可合并。
--
-- ## 回滚
--
-- `scripts/migrations/rollback/` 下有配套脚本；本表无 FK，代价为 0。

BEGIN;

-- --------------------------------------------------------------------------- #
-- 前置守卫：确认没有已经带前缀的行（重复执行本脚本会撞上这个守卫）
-- --------------------------------------------------------------------------- #
DO $$
DECLARE
    already_prefixed integer;
BEGIN
    SELECT count(*) INTO already_prefixed
    FROM public.cpt_structure_event
    WHERE structure_id LIKE 'cn:%' OR structure_id LIKE 'crypto:%';

    IF already_prefixed > 0 THEN
        RAISE EXCEPTION
            '已有 % 行带市场前缀 —— 本迁移不能重复执行（否则会变成 cn:cn:...）',
            already_prefixed;
    END IF;
END $$;

-- --------------------------------------------------------------------------- #
-- 1. 备份：把「旧 id → 新 id」映射落盘，回滚时照它反向 UPDATE
-- --------------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS public.cpt_structure_event_id_backup_20261001 (
    structure_id text PRIMARY KEY,
    new_id      text NOT NULL
);

INSERT INTO public.cpt_structure_event_id_backup_20261001 (structure_id, new_id)
SELECT
    e.structure_id,
    CASE
        WHEN EXISTS (
            SELECT 1
            FROM public.daily_bar d
            WHERE d.date = to_timestamp(split_part(e.structure_id, ':', 3)::bigint / 1000.0)
                      AT TIME ZONE 'UTC'
        )
        THEN 'cn:'    || e.structure_id
        ELSE 'crypto:' || e.structure_id
    END
FROM public.cpt_structure_event e
ON CONFLICT (structure_id) DO NOTHING;

-- --------------------------------------------------------------------------- #
-- 2. 事务内校验：改写前后行数必须一致，且不存在 id 冲突
-- --------------------------------------------------------------------------- #
DO $$
DECLARE
    before_count integer;
    after_count  integer;
    collide      integer;
BEGIN
    SELECT count(*) INTO before_count FROM public.cpt_structure_event;

    UPDATE public.cpt_structure_event e
    SET structure_id = b.new_id
    FROM public.cpt_structure_event_id_backup_20261001 b
    WHERE e.structure_id = b.structure_id;

    GET DIAGNOSTICS after_count = ROW_COUNT;

    IF after_count <> before_count THEN
        RAISE EXCEPTION '改写行数 % 与总数 % 不一致', after_count, before_count;
    END IF;

    -- 改写后不应再有不带前缀的行
    SELECT count(*) INTO collide
    FROM public.cpt_structure_event
    WHERE structure_id NOT LIKE 'cn:%' AND structure_id NOT LIKE 'crypto:%';

    IF collide > 0 THEN
        RAISE EXCEPTION '仍有 % 行没有市场前缀', collide;
    END IF;

    RAISE NOTICE '改写 % 行（cn: / crypto:）', after_count;
END $$;

COMMIT;
