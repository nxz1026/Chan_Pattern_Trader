-- R59（2026-10-09）回填 ``public.cpt_signal_event`` 的 structure_id / signal_id 股票代码。
--
-- ## 背景（审计 S3）
--
-- ``signal_id = f"{signal_type}:{level}:{structure_id}"``，而
-- ``structure_id = f"level{level}:{center_id}"``，``center_id`` 取
-- ``zhongshu.bi_ids[0]`` —— 代表 id 由 ``reference_chanlun`` / ``native_chanlun``
-- 生成 ``bi:{i}``，**纯位置下标、与股票无关**。而 ``load_previous_signal`` /
-- ``latest_status`` 都**只按 signal_id 查**（``WHERE signal_id = %s``）。
--
-- 上线前只读实测：库内 45 只票 / 72 行事件里，**12 组 signal_id 被两只票共用**
-- （撞号率 27%）。例：``first_buy:5:level5:bi:8`` 同时属于 000002 与 600707。
-- 后果：A 写 confirmed 后 B 读到 A 的状态，``record_signal_event`` 的
-- ``status == prev_status`` 提前返回 ⇒ B 的真实跃迁被抑制；交易机
-- ``load_trade_decisions`` 还可能拿到另一只票的 price / signal_id。
--
-- ## 代码侧修法（同批提交）
--
-- ``derive_first_buy_facts`` / ``derive_first_sell_facts`` 新增 ``code`` 参数，
-- 经 ``_scoped_structure_id`` 产出 ``f"level{level}:{center_id}:{code}"``；
-- ``code`` 为空（旧调用方 / 纯结构单测）时保持旧格式。
--
-- ## 本迁移做什么
--
-- 把历史行改写成同一格式。谓词 ``signal_id NOT LIKE '%:' || code`` 只命中
-- 仍为旧格式的行：上一轮已给 ``:empty`` 兜底分支补过 code
-- （``first_buy:5:level5:empty:000002`` 以 ``:000002`` 结尾），不会被二次追加；
-- ``length(code) = 6`` 再挡一道脏 code。
--
-- ## 为什么可以直接改主键
--
-- 本表 append-only、无 ``UNIQUE(signal_id)``，改主键等于「重新分桶」：同一只票的
-- 旧格式行与新格式行不会聚合，状态机会从「首次评估」重新开始。这是**期望**
-- 行为 —— 旧行本身就是串号的，不能作为可信的 ``previous``。
--
-- 迁移风格：可重复跑（幂等谓词）。
--
-- ## 核对（跑完可选执行）
--
--   SELECT signal_id, count(DISTINCT code) AS codes
--     FROM public.cpt_signal_event GROUP BY signal_id HAVING count(DISTINCT code) > 1;
--
-- 期望：0 行。
--
-- ## 已做过的复跑验证（scratch 库，5 行夹具）
--
-- 谓词有三个分支，光看 SQL 容易看漏，故在一次性库里实证过（R59 落地时）：
--
--   1. 撞号两行（同 ``bi:8``，code 000002 / 600707）⇒ 各自补上自己的 code，
--      ``signal_id`` 与 ``structure_id`` **都**变；
--   2. 上一轮 ``:empty`` 分支已补过 code 的行（``...:empty:000002``）⇒ 不动；
--   3. 脏 code（``'ABC'``，撞 ``length(code) = 6``）⇒ 不动；
--   4. 已是新格式的行（``...:bi:8:002119``）⇒ 不动；
--   5. **重跑本文件 ⇒ ``UPDATE 0``**（幂等），撞号查询 0 行。
--
-- 生产库只读 dry-run（上线前）：72 行中 71 行会被改写，改写后撞号 0 组。

BEGIN;

UPDATE public.cpt_signal_event
   SET structure_id = structure_id || ':' || code,
       signal_id    = signal_id    || ':' || code
 WHERE length(code) = 6
   AND code ~ '^[0-9]{6}$'
   AND signal_id NOT LIKE '%:' || code;

COMMIT;
