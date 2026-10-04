-- 因子口径纪元标记（R45）。
--
-- ## 为什么需要
--
-- 2026-10-03 两次切表把 `asel.ref_adjust_factor` 从「tx:fqkline 逐日比值」
-- 换成「公司行动重算」（R44/R45）。切换**不可逆**，且后果是：
--
--     切表前记录的 41 条信号里，29 条在切表后变成 `invalidated`
--     （结构被破坏），12 条仍是 `structure_ready`。
--
-- 那些 `invalidated` **不是**「信号失败了」，而是「结构在换口径后重算，
-- 与旧口径下的判断不一致」。两件事在没有标记的情况下**长得一模一样** ——
-- 于是下一个看到「信号突然失效」的人会当成 bug 排查一轮。
--
-- 这与 `run_metric.py` 已经在做的 `config_hash` 比对是同一个问题的两种形态：
-- 那个检测**运行历史**的口径漂移，这个标记让**信号事件流**也能区分。
--
-- ## 为什么是「一张单行表」而不是给每行加列
--
-- 加列会有一个**新旧混态**的坑：历史 41 行要回填，而新写入的行靠
-- `signal_event_store.record_signal_event` 赋值 —— 只要有一处漏了，表里
-- 就同时存在「新行有纪元 / 老行没纪元」两种状态，比没有更糟。
--
-- 而口径切换**本质上是一个时间点**：`cpt_signal_event` 本来就有
-- ``transition_time`` / ``created_at``，所以「这条事件属于哪个纪元」是
-- **可推导**的，不需要逐行存储。单行表只回答一个问题：切换发生在哪一刻。
--
-- 判据（可复现，不硬编码 41 个 id）：
--     事件 created_at < switched_at  ⇒ 旧口径
--     事件 created_at >= switched_at ⇒ 新口径
--
-- ## 部署顺序
--
-- 纯新增表，**不改动既有表结构**，所以不需要停服、不需要与代码同批部署。
-- 早于切表执行它只是多一行记录；晚于切表执行则那条记录描述的是**已知**的
-- 历史事实 —— 两种顺序都不会让系统出错。

CREATE TABLE IF NOT EXISTS public.cpt_factor_epoch (
    id             smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- 单行表
    switched_at    timestamptz NOT NULL,
    old_source     text NOT NULL,
    new_source     text NOT NULL,
    old_match_rate numeric,          -- 切前台阶匹配率（口径 A 实测）
    new_match_rate numeric,          -- 切后台阶匹配率（口径 B 实测）
    note           text NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.cpt_factor_epoch IS
    '因子口径纪元：switched_at 之前写入的信号/结构事件属于 tx:fqkline 口径，'
    '之后属于公司行动重算口径。单行表；不要 DELETE。';

COMMENT ON COLUMN public.cpt_factor_epoch.switched_at IS
    '口径切换时刻。判据：事件 created_at < switched_at 即旧口径。';
