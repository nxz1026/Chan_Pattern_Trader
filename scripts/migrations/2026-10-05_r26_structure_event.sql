-- R26（2026-10-01）新建结构事件表 ``public.cpt_structure_event``。
--
-- ## 补的是 rules.md §8.6 三层模型里缺的那一层
--
-- §8.6 的「当前状态 + 不可变事件 + 信号」：
--
-- - **信号**层：R21 已有 ``public.cpt_signal_event``；
-- - **结构**层：**本表**（此前 ``StructureEvent`` 全仓零生产者，``snapshot.events``
--   在生产里恒为 ``[]``）；
-- - **当前状态**：**不建表**，由事件流派生（同 ``structure_id`` 的最新一条）。
--
-- ## 为什么不建 structure_state 表
--
-- 两张表必然出现「状态表说 A、事件表说 B」的不一致，而事件流是唯一真相。
-- R21 的 ``cpt_signal_event`` 已经是这个形态（没有配 signal 状态表），
-- 这里保持一致。代价是「当前状态」要查一次 —— 但那个查询走
-- ``(structure_id, revision DESC)`` 索引，成本可忽略。
--
-- ## 列的取舍
--
-- ``status`` 冗余自 payload，但是**查询目标**：「某结构现在是什么状态」直接
-- ``WHERE structure_id=%s ORDER BY revision DESC`` 即可，不需要解 jsonb。
-- 这与 R21 的 ``cpt_signal_event.status`` 同一取舍。
--
-- ``payload`` 存整份 ``StructureState``（jsonb），因为事件要能**独立重放**：
-- 只存 diff 的话，读最新状态就得回放全链。

BEGIN;

CREATE TABLE IF NOT EXISTS public.cpt_structure_event (
    id           bigserial PRIMARY KEY,       -- append-only 顺序
    structure_id text NOT NULL,              -- 确定性 id：f"{kind}:{level}:{start_time}"
    event_type   text NOT NULL CHECK (event_type IN (
                      'created', 'updated', 'confirmed',
                      'reclassified', 'invalidated', 'closed')),
    status       text NOT NULL CHECK (status IN (
                      'forming', 'confirmed', 'invalidated', 'open_end')),
    revision     integer NOT NULL CHECK (revision >= 1),
    payload      jsonb,                      -- 该版本的完整 StructureState
    occurred_at  timestamptz NOT NULL,       -- 事件本身的时刻（非写入墙钟）
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- 核心查询：某结构的当前状态（最新一条）。也覆盖 diff 时的批量读。
CREATE INDEX IF NOT EXISTS idx_cpt_structure_event_sid_rev
    ON public.cpt_structure_event (structure_id, revision DESC);

-- 「这个结构的事件时间线」面板
CREATE INDEX IF NOT EXISTS idx_cpt_structure_event_occurred
    ON public.cpt_structure_event (occurred_at DESC);

-- 「某级别/某类型最近发生了什么」
CREATE INDEX IF NOT EXISTS idx_cpt_structure_event_kind_level
    ON public.cpt_structure_event (event_type, occurred_at DESC);

COMMENT ON TABLE public.cpt_structure_event IS
    'CPT 结构事件流（append-only）。R26。当前状态 = 同 structure_id 的最新一条。';
COMMENT ON COLUMN public.cpt_structure_event.structure_id IS
    'f"{kind}:{level}:{start_time}" —— 确定性生成（domain 零时钟零随机），故同输入必同 id，幂等重放成立。';
COMMENT ON COLUMN public.cpt_structure_event.revision IS
    '同一 structure_id 下的递增序号。**只在真的产生事件时 +1** —— 无变化的轮次不占号。';
COMMENT ON COLUMN public.cpt_structure_event.status IS
    '冗余自 payload，但它是查询目标：解 jsonb 才能回答「现在什么状态」不值当。';

COMMIT;
