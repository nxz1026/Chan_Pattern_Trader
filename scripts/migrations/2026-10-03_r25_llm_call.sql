-- R25（2026-10-01）新建 LLM 调用审计表 ``public.cpt_llm_call``。
--
-- ## 为什么是 append-only 的「调用流水」而不是结果表
--
-- 缠论结构解释是**旁路增强**：LLM 挂了看板照常出图（architecture.md §4.1 约束 3
-- 「可关闭」）。所以这里只记「谁在什么时候问了什么模型、拿到什么结果」，
-- 不参与任何领域计算。
--
-- ## 不写 public.llm_call_log
--
-- 那张表**是别的项目的**：实测 10 行真实数据，``purpose='stock-diagnosis'``、
-- ``model='sensenova-6.8-flash-lite'``，owner=postgres，无注释。CPT 往里写会
-- 污染别人的审计流。R24 确立的边界：CPT 只碰 ``public.cpt_*``。
--
-- ## 列的取舍（沿用 R23 的纪律：填不满 / 能现抽的列一律不加）
--
-- **没有 cost_est**。实测 agnes 免费档 ``x-litellm-response-cost-*`` 恒为 0.0
-- （服务端是 LiteLLM），一列永远为 0 的字段就是冗余。将来换付费 provider 再
-- ``ALTER TABLE ADD COLUMN``。
--
-- **没有 request_json**。提示词模板在代码里（``cpt/llm/prompts.py``），
-- 落一份完整 prompt 既是重复、又会把结构 JSON 复制进审计表；用
-- ``request_hash`` 代替 —— 它同时是缓存键与幂等依据。
--
-- **status 是单列枚举而不是多个布尔**：限流（rate_limited）不是错误、
-- 被重启打断（interrupted）也不是错误，混进 error 会让看板天天报红。

BEGIN;

CREATE TABLE IF NOT EXISTS public.cpt_llm_call (
    call_id       text PRIMARY KEY,          -- 幂等键（uuid4 hex）
    purpose       text NOT NULL,             -- explain_structure / summarize_diff / annotate
    subject_id    text,                      -- 被解释对象的业务 id（结构 id 等），只作审计
    status        text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'ok', 'error',
                                      'rate_limited', 'interrupted')),
    request_hash  text NOT NULL,             -- 提示词规范化后的 sha256（缓存键 + 幂等）
    result_text   text,                      -- 结果正文；失败为 NULL
    error_text    text,                      -- 失败原因 / 退避说明
    model         text,                      -- 服务端回报的模型名
    prompt_tokens integer,
    completion_tokens integer,
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz               -- ok / error / interrupted 时写入
);

-- 「最近若干次调用」面板（按时间倒序）
CREATE INDEX IF NOT EXISTS idx_cpt_llm_call_created_at
    ON public.cpt_llm_call (created_at DESC);

-- 同一对象的历次解释（「这个中枢被解释过几次」）
CREATE INDEX IF NOT EXISTS idx_cpt_llm_call_subject
    ON public.cpt_llm_call (subject_id, created_at DESC)
    WHERE subject_id IS NOT NULL;

-- 幂等去重：同 (purpose, request_hash) 已在跑就不再排一次
CREATE UNIQUE INDEX IF NOT EXISTS idx_cpt_llm_call_request_hash
    ON public.cpt_llm_call (purpose, request_hash)
    WHERE status IN ('queued', 'running', 'ok');

COMMENT ON TABLE public.cpt_llm_call IS
    'CPT LLM 调用审计（append-only 流水）。R25。旁路增强，不参与领域计算。';
COMMENT ON COLUMN public.cpt_llm_call.status IS
    'queued/running/ok/error/rate_limited/interrupted。限流与被重启打断都不是 error。';
COMMENT ON COLUMN public.cpt_llm_call.request_hash IS
    '提示词规范化后的 sha256：缓存键 + 幂等依据；不落完整 prompt（模板在代码里）。';
COMMENT ON COLUMN public.cpt_llm_call.error_text IS
    'ok 时为 NULL。rate_limited 时存 "retry_in=<秒> attempt=<n>"，便于 UI 显示退避进度。';

COMMIT;
