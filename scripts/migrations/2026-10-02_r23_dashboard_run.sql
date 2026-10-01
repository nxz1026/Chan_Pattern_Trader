-- R23（2026-10-01）新建 dashboard 运行持久化表 ``public.cpt_dashboard_run``。
--
-- ## 为什么新建
--
-- ``/api/dashboard/compare?left=&right=`` 与 ``/api/dashboard/multi-run?run_ids=``
-- 接收的 ``run_id`` 必须能在**进程重启后**查到本体；当前实现是
-- ``cpt.application.dashboard_runs._RUN_BODIES``（``deque``，``maxlen=50``，
-- **进程级**），重启即空 → 用户重启后报 ``run_body_unavailable``。
--
-- 用户诉求（2026-09-30）：「跨重启可比」。
--
-- ## 为什么只有 5 列
--
-- 12 列方案被明确叫停：``symbol`` / ``interval_ms`` / ``bar_count`` /
-- ``config_hash`` / ``source`` / ``created_at`` 全部能从 ``snapshot`` jsonb 现抽
-- （``snapshot->'market'->>'symbol'`` 等）。冗余列带来的是**一致性问题**——本体与
-- 冗余列一旦不一致，没人知道该信哪个。
--
-- 将来真出现「按 symbol 查全部 run」这类高频查询，再 ``ALTER TABLE ADD COLUMN``
-- + 回填，不要在第一版就猜。
--
-- ## 为什么主键是 run_id（text）
--
-- ``run_id`` 在 ``cpt.application.dashboard_runs.record_run`` 里的口径是
-- ``runtime.run_id or dataset_hash``：同值稳定、跨进程同值（fixtures / realtime /
-- demo 三种模式都能产出）。把 ``run_id`` 提为 PRIMARY KEY，天然就有了
-- 「同一份快照写多次只算一次」的去重——调用点用 ``ON CONFLICT DO NOTHING``。
--
-- ## 为什么有 body_recorded 而不是「非空即真」
--
-- ``RUN_BODY_MAX_BYTES = 4_000_000`` 闸门（见 ``dashboard_runs.py``）保留：一次
-- 脏请求（把整段原始行情塞进 snapshot）不该把 PG 的 jsonb 字段撑爆。Python 侧先
-- 用 ``json.dumps(...).encode("utf-8")`` 测字节，超限则 ``body=None``、
-- ``body_recorded=false``。这样 ``/compare`` 仍能查到**索引行**（``run_id`` +
-- ``dataset_hash``）用于字段级 diff，但拿不到本体时照样走
-- ``run_body_unavailable`` 降级，与 in-process ring 语义一致。
--
-- ## append-only + 不自动 GC
--
-- 1. 读路径草案是 ``WHERE run_id IN (...)`` 或 ``WHERE dataset_hash = ...``，
--    不需要全表扫描；表大小只影响 vacuum，不影响查询延迟。
-- 2. realtime 30s 一轮 ≈ 2,880 行/天，1 月 ≈ 86k 行，jsonb 平均 30KB
--    → 2.5 GB，PG vacuum 后稳定。**本仓不引入自动 GC**（避免在 HTTP 请求路径上
--    跑大 SQL）。运维想清理就手动：
--    ``DELETE FROM public.cpt_dashboard_run WHERE generated_at < now() - interval '7 days'``。
-- 3. 事件流天然 append-only，与 ``public.cpt_signal_event``（R21）口径一致。
--
-- ## 与 in-process ring 的关系
--
-- 两套**并存**：ring 留给 hot-path（30s 内同 ``dataset_hash`` 命中同一份缓存
-- snapshot 时快路径不写库），表留给 cold-path（重启后查历史）。
-- ``record_run`` 的调用顺序是 ``ring.append()`` → ``db.upsert ON CONFLICT DO NOTHING``，
-- 两者**独立失败**——ring 写失败不影响 HTTP；表写失败也不影响 ring，
-- 区别只是「重启后能不能查到」。
--
-- ## 幂等
--
-- ``CREATE TABLE`` / ``CREATE INDEX IF NOT EXISTS``，与 R20 / R21 同风格，可重复跑。

BEGIN;

CREATE TABLE IF NOT EXISTS public.cpt_dashboard_run (
    run_id        text PRIMARY KEY,           -- 业务主键 = runtime.run_id or dataset_hash
    dataset_hash  text NOT NULL,              -- 同数据集历史查询 / 索引；缺失时存 ''（见模块说明）
    generated_at  timestamptz NOT NULL,       -- 时间排序 + WHERE 过滤（必须独立列 + 索引）
    body_recorded boolean NOT NULL,           -- 4MB 闸门状态
    snapshot      jsonb                       -- 本体；超闸门为 NULL
);

-- 查某数据集的全部历史（按时间倒序，给前端运行搜索界面用）
CREATE INDEX IF NOT EXISTS idx_cpt_dashboard_run_dataset_hash
    ON public.cpt_dashboard_run (dataset_hash, generated_at DESC);

-- 「最近若干次运行」面板
CREATE INDEX IF NOT EXISTS idx_cpt_dashboard_run_generated_at
    ON public.cpt_dashboard_run (generated_at DESC);

COMMENT ON TABLE public.cpt_dashboard_run IS
    'CPT dashboard 运行持久化（append-only）。每条 = 一次 record_run 命中（含去重）。'
    'body_recorded=false 通常因为超 4MB 闸门。';
COMMENT ON COLUMN public.cpt_dashboard_run.run_id IS
    '业务主键 = runtime.run_id or dataset_hash（见 dashboard_runs.record_run）。';
COMMENT ON COLUMN public.cpt_dashboard_run.dataset_hash IS
    'reproducibility.dataset_hash；该字段缺失时存空串而非 NULL（列定义为 NOT NULL）。';
COMMENT ON COLUMN public.cpt_dashboard_run.generated_at IS
    'reproducibility.generated_at（Unix 毫秒 → timestamptz）；缺失时回落到写入墙钟。';
COMMENT ON COLUMN public.cpt_dashboard_run.body_recorded IS
    'true=本体已落库；false=超 RUN_BODY_MAX_BYTES 闸门被拒，compare 会降级为 run_body_unavailable。';
COMMENT ON COLUMN public.cpt_dashboard_run.snapshot IS
    'snapshot 本体 jsonb；超闸门时为 NULL，对应 body_recorded=false。';

COMMIT;
