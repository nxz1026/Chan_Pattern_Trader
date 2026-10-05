-- R45：推荐留痕表（让「切表/改口径后推荐变了没」从推断变成可看）
--
-- 为什么需要它
--   推荐是 R45 才有的功能，**每次请求实时算、零留痕**。
--   而因子表在 R45 一天内切了 **4 次**、口径变过 2 次 ——
--   「切表前推荐长什么样」这个问题**当时没有答案**，现在也补不回来。
--   ⇒ 至少让**今后的**这类变化**看得见**。
--
-- 形状
--   code / level      标的与周期
--   action            动作（buy/sell/watch/hold）
--   price             **不复权**参考价（能挂单的那个，不是后复权价）
--   signal_status     当时的信号状态（confirmed/candidate/alert/invalidated/none）
--   signal_type       当时的信号类型
--   headline / reason 人类可读的结论与依据（留原文，事后不用重算）
--   data_bars         当时的 K 线根数（数据够不够判断）
--   factor_epoch      当时的**因子口径纪元**（= cpt_factor_epoch.switched_at）
--                     ⇒ 这就是「切表前后」的分组依据，与看板的 history 区同源
--
-- 不建索引在 (code, created_at) 上：实测每天每票只写几行、
-- 表规模远小于 cpt_run_metric。真要查全表时再说（这与 R39 砍索引的纪律一致）。

CREATE TABLE IF NOT EXISTS public.cpt_recommendation (
    id            bigserial PRIMARY KEY,
    code          text        NOT NULL,
    level         text        NOT NULL DEFAULT '',
    action        text        NOT NULL,
    price         double precision,
    signal_status text        NOT NULL DEFAULT 'none',
    signal_type   text        NOT NULL DEFAULT '',
    headline      text        NOT NULL DEFAULT '',
    reason        text        NOT NULL DEFAULT '',
    data_bars     integer     NOT NULL DEFAULT 0,
    factor_epoch  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.cpt_recommendation IS
    'R45 新增：结构判断推荐的历史留痕。用于回看「切表 / 改口径前后推荐变了没」。'
    '写入是 best-effort —— 留痕失败不该让推荐接口 500。';
COMMENT ON COLUMN cpt_recommendation.factor_epoch IS
    '当时的因子口径切换点（取自 cpt_factor_epoch.switched_at）；为空表示当时还没有纪元。';
COMMENT ON COLUMN cpt_recommendation.price IS
    '**不复权**参考价（能挂单的那个）。后复权价另存于 recommendation 的 price 字段，不进表。';
