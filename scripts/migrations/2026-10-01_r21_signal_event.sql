-- R21（2026-10-01）新建 CPT 自有信号事件表 ``public.cpt_signal_event``。
--
-- ## 为什么新建
--
-- ``public.signal`` 162 行 / 154 只 code，``status`` 唯一取值 ``SUGGESTED``，
-- 无 ``structure_id``、无 ``previous``、无四态；写入者是 emotion-core 不是 CPT。
-- ``public.signal_outcome`` 0 行。CPT 全仓**零信号/事件/状态持久化**（grep
-- ``INSERT INTO|UPDATE|DELETE FROM`` 仅 ``asel.ref_adjust_factor`` 一处，R20 补因子）。
--
-- 状态机五态（``structure_ready`` / ``alert`` / ``candidate`` / ``confirmed`` /
-- ``invalidated``）从未落库 → 面板 ``signal`` 恒 ``null``、Phase 6 五项全 0 命中。
-- 新建 append-only 事件流，每条 = 一次状态跃迁；当前状态 = 同 signal_id 最新事件。
--
-- ## 为什么 append-only
--
-- 1. 事件流天然可回放：从空表按 ``id`` 升序重放，可重建任意时刻的信号状态。
-- 2. 不覆盖历史：``UPDATE`` 会抹掉前一状态，无法追溯「什么时候从 alert 变 confirmed」。
-- 3. 与 ``docs/rules.md`` §8.6「只追加语义」一致。
--
-- ## 为什么存完整 Signal 字段
--
-- 当前状态 = 最新事件。完整存 ``center_ids`` / ``divergence_status`` / 四时间戳
-- 是为了 ``load_previous_signal()`` 能重建一个 ``Signal`` 对象喂给
-- ``assess_first_buy(previous=...)``，不用额外查 ``public.signal``。
--
-- ## 幂等与去重
--
-- 主键 ``id``（bigserial）保证 append-only 顺序。
-- 业务去重由写入层负责：同 ``(signal_id, status)`` 不重复 append
-- （30s 轮询不产生垃圾行）。本表不加 ``UNIQUE(signal_id, status)``，
-- 因为同一 status 在「失效→重新准备」时可以合法地再次出现。

BEGIN;

CREATE TABLE IF NOT EXISTS public.cpt_signal_event (
    id              bigserial PRIMARY KEY,
    signal_id       text NOT NULL,
    code            text NOT NULL,
    signal_type     text NOT NULL CHECK (signal_type IN ('first_buy', 'first_sell')),
    level           integer NOT NULL,
    structure_id    text NOT NULL,
    prev_status     text,                          -- NULL = 首次评估
    status          text NOT NULL CHECK (status IN (
                        'structure_ready', 'alert', 'candidate',
                        'confirmed', 'invalidated'
                    )),
    transition_time timestamptz NOT NULL,          -- 事件时间（Unix ms → timestamptz）
    price           double precision,
    source_revision integer NOT NULL DEFAULT 0,
    center_ids      text[] NOT NULL DEFAULT '{}',
    divergence_status text NOT NULL DEFAULT 'not_checked'
                        CHECK (divergence_status IN (
                            'not_checked', 'not_detected', 'detected'
                        )),
    alert_time      timestamptz,
    candidate_time  timestamptz,
    confirmed_time  timestamptz,
    invalidated_time timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()  -- 写入墙钟
);

-- 按 signal_id 查最新事件（load_previous_signal）
CREATE INDEX IF NOT EXISTS idx_cpt_signal_event_signal_id_id
    ON public.cpt_signal_event (signal_id, id DESC);

-- 按 code 查某只票的全部事件
CREATE INDEX IF NOT EXISTS idx_cpt_signal_event_code_id
    ON public.cpt_signal_event (code, id DESC);

COMMENT ON TABLE public.cpt_signal_event IS
    'CPT 信号事件流（append-only）。每条 = 一次状态跃迁；当前状态 = 同 signal_id 最新事件。';

COMMIT;
