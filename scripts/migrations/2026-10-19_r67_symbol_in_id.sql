-- R67（2026-10-19）给 `structure_id` 补标的一段：`{market}:{symbol}:{kind}:{level}:{start_time}`。
--
-- 审计 P0-2。这是 R27-4 那次修正的**同一个病的第二次发作**。
--
-- ## 病是什么
--
-- R26 建表时 id 是 `{kind}:{level}:{start_time}`，R27-4 补了 `market`，
-- 但**没补 symbol**。而 `structure_id` 是唯一的合并判据（同 id = 同一个结构，
-- revision 沿同一条链递增）—— 少一段，不同标的就在同一条链上互相踩。
--
-- 加密侧侥幸没爆：全historically只跑过 BTCUSDT（`deploy/env/cpt-dashboard.env`
-- 的 `CPT_SYMBOL=BTCUSDT`，`public.cpt_run_metric` 的 crypto 段 symbol 取值集合
-- 就是 `{'BTCUSDT'}`）。
--
-- **A 股侧必然爆**，因为 A 股全是日线，`start_time` 就是 K 线时间：
-- 每只股票的时间轴完全重合。实测（本迁移编写时的只读盘点）：
--
--   | 证据                                   | 数值                                   |
--   |----------------------------------------|----------------------------------------|
--   | `cn:bi:5:1782172800000` 一个 id 的行数  | **368 行**                             |
--   | 该 id 下的 `end_time` **回退**次数      | **174 次**（单一结构只应单调后移）      |
--   | 该 id 实际横跨的不同标的数              | **74 只**                               |
--   | `(structure_id, revision)` 重复的组数   | **592 组**（同 id 同号 = 结构性坏了）   |
--   | cn 侧 id 数 / 标的数                    | 703 / 207 = 3.4（应≈207 只各自一套）    |
--   | 面板 `recent_events` 前 10 条           | 全部是未来日期（2026-12-14 起）         |
--
-- ⇒ cn 侧的 4 段行**不是一个「不知道 symbol」的历史，而是一份被证明互相污染的
--   历史**：一个 id 下 74 只票的笔混在一条 revision 链上，`revision` 冲突 592 组。
--   它无法被「补一个 symbol」拯救 —— 因为每个 id 同时对应对个 symbol。
--
-- ## 处理策略（按市场分开，因为两者的可归属性是**类别不同**，不是程度不同）
--
-- **crypto：回填。** 历史上只有 BTCUSDT 一个标的，归属 100% 确定，
-- 回填后是全真的历史（1515 个 id、2405 行，1.59 行/id —— 与单标的正常水位吻合）。
-- 回填 = 把 `crypto:` 前缀换成 `crypto:BTCUSDT:`。
--
-- **cn：删除（先备份）。** 理由不是「查不到」，而是**查得到且证明是坏的**：
-- 一个 id 横跨 74 只票。若按「同批次 created_at 最近的水位行」批量反推，
-- 只能覆盖 **75.2%** 的行（13326/17716，窗口 10s），剩下 24.8% 要么删、
-- 要么猜 —— 而混入 25% 猜出来的行，会让这条链**看起来**像真的：新代码首次
-- 运行时会把它们当成自己的前态做 diff，于是基于伪造的前态产生事件。
-- 「未知」可以处理，「看起来对但错」不能。所以整段删掉，让 cn 从下次快照
-- 干净地重新开始（首个 revision=1 的 `created` 即真前态）。
--
-- 这与 R27-4 定下的方向一致：**宁可重复，不可合并**。合并会让状态机从错误的
-- 前态继续推进；重复只是多一条 `created`，可辨认、可清理。
--
-- **测试污染行：删除。** 5 段里 `created_at = 2026-10-19 01:16:05.604669+00`
-- 那 84 行是测试写的（`tests/test_ashare_placeholder_rows.py` 的假 client
-- `_get_conn()` 返回 `None`，recorder 于是自开生产连接）。它们不是历史。
-- 该渗漏已在代码侧修掉（`cpt/application/persist_gate.py`）。
--
-- **未来时间戳行：随 cn 一起消失。** 53 行 `occurred_at > now()`（最远 2026-12-14）
-- 全是 cn 段行（4 段 30 + 测试 5 段 23），被上面两条覆盖。
--
-- **它们也来自上面那个测试**，不是真实 A 股数据 —— 这一点初判时搞错过，记下来：
-- 它们带 `cause`（走过 A 股记录路径）、created_at 与真实历史批次同段
-- （`2026-10-06 10:29:59.642405` / `2026-10-09 00:26:26.636845`），所以像真数据。
-- 决定性反证是**复算**：`tests/test_ashare_placeholder_rows.py::_bars(200)` 从
-- `2026-06-01` 起连续 200 个**日历日**（末端 2026-12-17，比当时晚两个多月），
-- 把同一序列喂进 `detect_fractals`，输出的未来分型日期与库里那 16 个
-- **逐日完全一致**。⇒ 合成 bar 取在未来 ⇒ `occurred_at`（取 `end_time`）在未来。
-- 详见 `docs/known-traps.md` #58；测试的 `_bars()` 起点已同时改成锚在「今天」。
-- 本迁移只负责让它们不再出现在面板顶部。
--
-- ## 与代码的部署顺序（不能错）
--
-- 与 R27-4 同款：**停服 → 跑本迁移 → 起新代码**。
-- 中间任何时刻新旧格式并存，diff 都会把结构全当新结构（每轮重复写 `created`，
-- revision 从 1 重开）。本迁移是「一次性」的：写完之后 4 段行必须为 0。
--
-- ## 回滚
--
-- `scripts/migrations/rollback/2026-10-19_r67_symbol_in_id_rollback.sql`。
-- 本表无外键（实测 `pg_constraint` 里没有任何约束指向它），所以两张备份表
-- 足以完整还原：`_backup_20261019`（crypto 改写映射）+ `_dropped_20261019`（cn/测试行整行）。

-- --------------------------------------------------------------------------- #
-- P0-2 之外，本迁移顺带收口的第二件事：**并发双写**
-- --------------------------------------------------------------------------- #
-- 盘点时发现 4 段行里有 **417 组** `(structure_id, revision)` 重复 —— 而这个
-- 数字**与 symbol 无关**（加密侧单标的也有），所以它是另一个病：
--
--   2026-10-08 09:37:19.891475 与 .910071（相隔 **18.6 ms**）各写了 **408 行**，
--   两批的 payload **逐字节相同**。两个写入者同时 diff、都看到「前态为空」，
--   于是都写出 revision=1 的 `created`。10-12 另有 9 组。
--
-- 根因：`record_structure_events` 是「读前态 → diff → 写」三步，**中间没有锁**，
-- 而这条链上没有任何东西阻止第二个人同时进来。表上也没有
-- `(structure_id, revision)` 的唯一约束 —— 于是撞车的结果被**静默接受**了。
--
-- 修法两层：
--   1. **一次性去重**（下面第 5 步）：两批 payload 逐字节相同 ⇒ 保留 `min(id)`
--      是真正无损的（不是「选一个保留」，是删掉一个一模一样的副本）。
--   2. **加唯一索引**（第 7 步）：让下一次撞车直接**报错**而不是静默写入。
--      这是根治 —— 没有它，去重只是把地上的水擦掉。
--
-- ⚠️ 唯一索引会改变运行期行为：并发写入从此会抛唯一键冲突。recorder 是
-- **best-effort**（``except Exception`` 记 warning 后返回空），所以表现是
-- 「这一轮少写一批事件」而不是崩溃 —— 与它现有的降级语义一致，可接受。

BEGIN;

-- --------------------------------------------------------------------------- #
-- 前置守卫 1：本迁移不能重复执行
-- --------------------------------------------------------------------------- #
-- ⚠️ 判据必须**排除那 84 行测试污染**（`cn:600519:*`）：它们本来就是 5 段，
-- 若照「有 5 段行就拒绝」写，本迁移一上来就被自己的测试垃圾挡住。
-- 排除之后，5 段行只剩「真·新格式」，那才是「已经跑过本迁移」的信号。
DO $$
DECLARE
    five_seg integer;
BEGIN
    SELECT count(*) INTO five_seg
    FROM public.cpt_structure_event
    WHERE array_length(string_to_array(structure_id, ':'), 1) = 5
      AND NOT (structure_id LIKE 'cn:600519:%'
               AND created_at = TIMESTAMPTZ '2026-10-19 01:16:05.604669+00');

    IF five_seg > 0 THEN
        RAISE EXCEPTION
            '已有 % 行是真·5 段（新格式）—— 本迁移不能重复执行', five_seg;
    END IF;
END $$;

-- --------------------------------------------------------------------------- #
-- 前置守卫 2：确认加密侧真的只有 BTCUSDT（这是回填 100% 成立的唯一依据）
-- --------------------------------------------------------------------------- #
DO $$
DECLARE
    syms text;
BEGIN
    SELECT string_agg(DISTINCT symbol, ',') INTO syms
    FROM public.cpt_run_metric
    WHERE market = 'crypto';

    IF syms IS DISTINCT FROM 'BTCUSDT' THEN
        RAISE EXCEPTION
            '加密侧水位表出现过非 BTCUSDT 的标的（%）—— 回填前提不成立，需人工裁定', syms;
    END IF;
END $$;

-- --------------------------------------------------------------------------- #
-- 1. 备份 A：逐行的「旧 id / 旧 payload → 新 id」映射（回滚照它反向还原）
-- --------------------------------------------------------------------------- #
-- 主键是**事件行自己的 id**，不是 structure_id —— 这一点必须想清楚：
--
-- 同一个 4 段 id 在表里通常有多行（revision 1、2、3…），**每行 payload 都不同**。
-- 若按 structure_id 去重着备份（`DISTINCT ON (structure_id)`），回滚时那个被挑中的
-- payload 会被写到该 id 的**所有**行上 —— 实测往返比对因此有 **479 行**对不上
-- （典型症状：`revision` 列是 1，payload 里却写着 2）。所以备份粒度必须是行。
--
-- `old_payload` 也是**必须**存的，不是冗余：见下方「R27-4 留下的旧伤」。
CREATE TABLE IF NOT EXISTS public.cpt_structure_event_id_backup_20261019 (
    id          bigint PRIMARY KEY,   -- public.cpt_structure_event.id
    old_id      text NOT NULL,
    new_id      text NOT NULL,
    old_payload jsonb,
    reason      text NOT NULL
);

INSERT INTO public.cpt_structure_event_id_backup_20261019
    (id, old_id, new_id, old_payload, reason)
SELECT
    e.id,
    e.structure_id,
    'crypto:BTCUSDT:' || split_part(e.structure_id, ':', 2) || ':'
        || split_part(e.structure_id, ':', 3) || ':' || split_part(e.structure_id, ':', 4),
    e.payload,
    'crypto 历史上只有 BTCUSDT（R67/P0-2 回填）'
FROM public.cpt_structure_event e
WHERE split_part(e.structure_id, ':', 1) = 'crypto'
  AND array_length(string_to_array(e.structure_id, ':'), 1) = 4
ON CONFLICT (id) DO NOTHING;

-- --------------------------------------------------------------------------- #
-- 2. 备份 B：cn 4 段行 + 测试 5 段行整行落盘（回滚直接 INSERT 回来）
-- --------------------------------------------------------------------------- #
-- ⚠️ 刻意**不用** `LIKE ... INCLUDING DEFAULTS`：那会把 `id bigserial` 的默认值
-- （`nextval('cpt_structure_event_id_seq')`）一起抄过来，于是备份表与被备份表
-- 共用同一个序列 —— 回滚时若漏给 id，就会从生产序列里取号、撞上在用主键。
-- 这里逐列写明、且**不设默认值**，让备份表与生产序列彻底解耦。
CREATE TABLE IF NOT EXISTS public.cpt_structure_event_dropped_20261019 (
    id           bigint      NOT NULL,       -- 原 id，回滚时按它插回
    structure_id text        NOT NULL,
    event_type   text        NOT NULL,
    status       text        NOT NULL,
    revision     integer     NOT NULL,
    payload      jsonb,
    occurred_at  timestamptz NOT NULL,
    created_at   timestamptz NOT NULL
);

INSERT INTO public.cpt_structure_event_dropped_20261019
    (id, structure_id, event_type, status, revision, payload, occurred_at, created_at)
SELECT e.id, e.structure_id, e.event_type, e.status, e.revision,
       e.payload, e.occurred_at, e.created_at
FROM public.cpt_structure_event e
WHERE (
        split_part(e.structure_id, ':', 1) = 'cn'
        AND array_length(string_to_array(e.structure_id, ':'), 1) = 4
      )
   OR (
        -- 测试污染：那一次 pytest 写入的 5 段行（同一毫秒批次，symbol=600519）
        array_length(string_to_array(e.structure_id, ':'), 1) = 5
        AND e.structure_id LIKE 'cn:600519:%'
        AND e.created_at = TIMESTAMPTZ '2026-10-19 01:16:05.604669+00'
      );

-- --------------------------------------------------------------------------- #
-- 3. 事务内校验：备份数与待改数一致，然后才动数据
-- --------------------------------------------------------------------------- #
DO $$
DECLARE
    n_backup        integer;
    n_crypto        integer;
    n_crypto_rows   integer;
    n_drop          integer;
    n_cn            integer;
    n_test          integer;
BEGIN
    SELECT count(*) INTO n_backup FROM public.cpt_structure_event_id_backup_20261019;
    -- 备份 A 是**逐行**的（主键 = 事件行自己的 id），所以这里比行数。
    SELECT count(*) INTO n_crypto FROM public.cpt_structure_event
     WHERE split_part(structure_id, ':', 1) = 'crypto'
       AND array_length(string_to_array(structure_id, ':'), 1) = 4;
    IF n_backup <> n_crypto THEN
        RAISE EXCEPTION '备份映射 % 行 ≠ 待回填 crypto 行 % 行', n_backup, n_crypto;
    END IF;

    SELECT count(*) INTO n_crypto_rows FROM public.cpt_structure_event
     WHERE split_part(structure_id, ':', 1) = 'crypto'
       AND array_length(string_to_array(structure_id, ':'), 1) = 4;

    SELECT count(*) INTO n_drop FROM public.cpt_structure_event_dropped_20261019;
    SELECT count(*) INTO n_cn FROM public.cpt_structure_event
     WHERE split_part(structure_id, ':', 1) = 'cn'
       AND array_length(string_to_array(structure_id, ':'), 1) = 4;
    SELECT count(*) INTO n_test FROM public.cpt_structure_event
     WHERE array_length(string_to_array(structure_id, ':'), 1) = 5
       AND structure_id LIKE 'cn:600519:%'
       AND created_at = TIMESTAMPTZ '2026-10-19 01:16:05.604669+00';
    IF n_drop <> n_cn + n_test THEN
        RAISE EXCEPTION '待删备份 % 条 ≠ cn % 条 + 测试 % 条', n_drop, n_cn, n_test;
    END IF;

    RAISE NOTICE '回填 crypto % 个 id（% 行）；删除 cn % 行 + 测试 % 行（均已备份）',
        n_crypto, n_crypto_rows, n_cn, n_test;
END $$;

-- --------------------------------------------------------------------------- #
-- 4. 回填 crypto（唯一键冲突在这里不可能：新 id 多一段，且旧 id 全无 symbol 段）
-- --------------------------------------------------------------------------- #
UPDATE public.cpt_structure_event e
SET structure_id = b.new_id
FROM public.cpt_structure_event_id_backup_20261019 b
WHERE e.id = b.id;

-- 同步 payload 里冗余的那份 id（`state_from_event` **优先读它**，
-- 不同步会让「列上的 id」与「payload 里的 id」不一致 —— 而后者是读接口的返回值）。
--
-- ⚠️ 这一步顺带治好了 R27-4 留下的旧伤：那 480 条 crypto 行的 payload.id
-- 还停在 3 段（连 `crypto:` 都没有）。它们此前没被谁发现，是因为
-- `latest_events` 按 `structure_id` 等值匹配、`state_from_event` 又只把
-- payload.id 当返回值的一个字段 —— 没有任何判断依据读它，所以错得**安静**。
-- 从本迁移起两者一致；回滚靠 `old_payload` 还原成原来那个错的样子。
UPDATE public.cpt_structure_event e
SET payload = jsonb_set(e.payload, '{id}', to_jsonb(e.structure_id))
WHERE split_part(e.structure_id, ':', 1) = 'crypto'
  AND array_length(string_to_array(e.structure_id, ':'), 1) = 5;

-- --------------------------------------------------------------------------- #
-- 5. 删除被证明互相污染的 cn 行与测试行
-- --------------------------------------------------------------------------- #
DELETE FROM public.cpt_structure_event e
WHERE (
        split_part(e.structure_id, ':', 1) = 'cn'
        AND array_length(string_to_array(e.structure_id, ':'), 1) = 4
      )
   OR (
        array_length(string_to_array(e.structure_id, ':'), 1) = 5
        AND e.structure_id LIKE 'cn:600519:%'
        AND e.created_at = TIMESTAMPTZ '2026-10-19 01:16:05.604669+00'
      );

-- --------------------------------------------------------------------------- #
-- 5b. 并发双写去重：同一 (structure_id, revision) 只留 min(id)
-- --------------------------------------------------------------------------- #
-- 前置断言：被删的那些必须与保留的那条 **payload 逐字节相同**。
-- 若不同，说明这不是「同一份结果写了两遍」，而是「两个不同的结果撞了号」——
-- 那不能靠删一条解决，必须停下来人工裁定。
--
-- ⚠️ 去重要**自己再备份一份**：删除发生在第 6 步（crypto 已改成 5 段）之后，
-- 此时备份 B 早已跑完（它只抓 cn 4 段 + 测试 5 段，抓不到这些 crypto 行）。
-- 少了这一步，回滚会静默少 417 行 —— 「回滚」变成「换一份差不多但更少的数据」，
-- 这正是 migrations 最容易骗过自己的地方。
CREATE TABLE IF NOT EXISTS public.cpt_structure_event_deduped_20261019 (
    id           bigint      NOT NULL,
    structure_id text        NOT NULL,
    event_type   text        NOT NULL,
    status       text        NOT NULL,
    revision     integer     NOT NULL,
    payload      jsonb,
    occurred_at  timestamptz NOT NULL,
    created_at   timestamptz NOT NULL
);

INSERT INTO public.cpt_structure_event_deduped_20261019
    (id, structure_id, event_type, status, revision, payload, occurred_at, created_at)
SELECT e.id, e.structure_id, e.event_type, e.status, e.revision,
       e.payload, e.occurred_at, e.created_at
FROM public.cpt_structure_event e
JOIN (
    SELECT structure_id, revision, min(id) AS keep_id
      FROM public.cpt_structure_event
     GROUP BY 1, 2 HAVING count(*) > 1
) d ON e.structure_id = d.structure_id AND e.revision = d.revision
WHERE e.id <> d.keep_id;

DO $$
DECLARE
    n_dup      integer;
    n_differing integer;
BEGIN
    SELECT count(*) INTO n_dup FROM (
        SELECT structure_id, revision FROM public.cpt_structure_event
         GROUP BY 1, 2 HAVING count(*) > 1) t;
    IF n_dup = 0 THEN
        RAISE NOTICE '无 (structure_id, revision) 重复，跳过去重';
        RETURN;
    END IF;

    SELECT count(*) INTO n_differing FROM (
        SELECT structure_id, revision, count(DISTINCT payload::text) np,
               count(DISTINCT event_type) ne
          FROM public.cpt_structure_event
         GROUP BY 1, 2 HAVING count(*) > 1) t
     WHERE np > 1 OR ne > 1;
    IF n_differing > 0 THEN
        RAISE EXCEPTION
            '有 % 组重复的 payload/event_type **不一致** —— 不是重复写，是撞号，需人工裁定',
            n_differing;
    END IF;

    RAISE NOTICE '并发双写：% 组 dupe，payload 全部一致，已备份待删行', n_dup;
END $$;

DELETE FROM public.cpt_structure_event e
USING (
    SELECT structure_id, revision, min(id) AS keep_id
      FROM public.cpt_structure_event
     GROUP BY 1, 2 HAVING count(*) > 1
) d
WHERE e.structure_id = d.structure_id
  AND e.revision = d.revision
  AND e.id <> d.keep_id;

-- --------------------------------------------------------------------------- #
-- 6. 终检：不存在 4 段行；无 revision 冲突；symbol 段非空
-- --------------------------------------------------------------------------- #
DO $$
DECLARE
    n_four   integer;
    n_empty  integer;
    n_odd    integer;
    n_dup    integer;
BEGIN
    SELECT count(*) INTO n_four FROM public.cpt_structure_event
     WHERE array_length(string_to_array(structure_id, ':'), 1) <> 5;
    IF n_four > 0 THEN
        RAISE EXCEPTION '仍有 % 行不是 5 段格式', n_four;
    END IF;

    SELECT count(*) INTO n_empty FROM public.cpt_structure_event
     WHERE split_part(structure_id, ':', 2) = '';
    IF n_empty > 0 THEN
        RAISE EXCEPTION '仍有 % 行的 symbol 段为空', n_empty;
    END IF;

    SELECT count(*) INTO n_odd FROM public.cpt_structure_event
     WHERE split_part(structure_id, ':', 1) = 'crypto'
       AND split_part(structure_id, ':', 2) <> 'BTCUSDT';
    IF n_odd > 0 THEN
        RAISE EXCEPTION 'crypto 段有 % 行 symbol 不是 BTCUSDT', n_odd;
    END IF;

    SELECT count(*) INTO n_dup FROM (
        SELECT structure_id, revision FROM public.cpt_structure_event
         GROUP BY 1, 2 HAVING count(*) > 1) t;
    IF n_dup > 0 THEN
        RAISE EXCEPTION '仍有 % 组 (structure_id, revision) 重复', n_dup;
    END IF;

    RAISE NOTICE '终检通过：5 段格式、symbol 非空、无 revision 冲突';
END $$;

-- --------------------------------------------------------------------------- #
-- 7. 加唯一约束（根治并发双写）
-- --------------------------------------------------------------------------- #
-- 这是本迁移**唯一让运行期行为发生变化**的一步：此后同一
-- `(structure_id, revision)` 第二次写入会抛唯一键冲突，而不是静默多插一行。
--
-- 幂等性：`CREATE UNIQUE INDEX IF NOT EXISTS` 可重复执行。
-- 若此前已有同名索引但定义不同，`IF NOT EXISTS` 会**静默跳过**（PG 按名字判），
-- 所以下面顺带断言它的确唯一 —— 一个非唯一的同名索引会让去重形同虚设。
CREATE UNIQUE INDEX IF NOT EXISTS uq_cpt_structure_event_sid_rev
    ON public.cpt_structure_event (structure_id, revision);

DO $$
DECLARE
    is_unique boolean;
BEGIN
    SELECT indisunique INTO is_unique
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
     WHERE c.relname = 'uq_cpt_structure_event_sid_rev';

    IF is_unique IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'uq_cpt_structure_event_sid_rev 存在但不是唯一索引 —— 去重的前提不成立';
    END IF;
    RAISE NOTICE '唯一索引已就位（并发双写从此报错而非静默写入）';
END $$;

COMMIT;

-- --------------------------------------------------------------------------- #
-- 迁移后（人工）验收
-- --------------------------------------------------------------------------- #
-- 1) 段数分布应只剩 5 段：
--      SELECT array_length(string_to_array(structure_id,':'),1), count(*)
--        FROM public.cpt_structure_event GROUP BY 1;
-- 2) 每个 id 只应有一个 revision 序列（无冲突）：
--      SELECT count(*) FROM (SELECT structure_id, revision
--                              FROM public.cpt_structure_event
--                             GROUP BY 1,2 HAVING count(*) > 1) t;   -- 期望 0
-- 3) 按标的分组后，行/id 比应回到小数量级：
--      SELECT split_part(structure_id,':',1), split_part(structure_id,':',2),
--             count(*), count(DISTINCT structure_id)
--        FROM public.cpt_structure_event GROUP BY 1,2 ORDER BY 3 DESC;
-- 4) 面板不再有未来日期霸顶：
--      SELECT occurred_at FROM public.cpt_structure_event ORDER BY occurred_at DESC LIMIT 5;
