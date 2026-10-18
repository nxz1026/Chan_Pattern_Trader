"""每轮计算的**运行水位 + 算法指纹**（``public.cpt_run_metric``）。

## 这张表为什么存在（R38）

owner 把日志分成两轨：

1. **运行日志** —— 数据是否完整、程序运行期间有没有问题；
2. **算法日志** —— 算法是否符合预期、结果是否偏移，给未来 Loop/LLM 做支撑。

R38 量完的现状是"第一轨只有半个（记了没人看、没告警、**没有水位**），第二轨基本
没有"。而两轨最缺的那个共同底座就是这张表：

- **数据完整性不能靠报错来发现**。``data_quality`` 只在响应时现算、没有历史序列，
  所以"数据什么时候开始不完整"根本查不出来 —— 出事才查，一定已经晚了。
  **水位**（最后一根 bar 时间 / bar 数 / 缺口数 / 因子覆盖率 / 快照 age）每轮落一行，
  才有"什么时候开始坏"的证据。
- **算法偏移需要一个可比基线**。一次算完，光有"我算了什么"没用，要有
  "这次的指纹是什么、结构计数是多少、和上一次/参照差多少"。R36 那次
  「装个 czsc 就能静默切生产后端」之所以查不出来，就是因为**没记 backend**。

## 一张表装两轨

``kind='run'`` 的行是每轮计算的水位+指纹（**高���率**，一条几百字节）；
``kind='inspection'`` 的行是每日巡检的结论（**低频**，一天一条）。巡检读的是最近
的 run 行，结论自己也落一行 —— 这样"看板上看到的"和"发到飞书的"是同一份数据。

## 边界

只存储、只 SELECT/INSERT；SQL 都在这里；**不 commit**（事务边界归调用方）。
写入失败**不抛**（观测数据不该拖垮计算路径）。

保留：默认按 ``observed_at`` 保留窗口（见 :func:`prune`），别让它长成第二份
``daily_bar``。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

_LOG = logging.getLogger(__name__)

#: 批次保存点名。固定一个即可：同一连接上重复 ``SAVEPOINT`` 同名会顶掉旧的，
#: 而同一连接本来就不可并发使用。
_SAVEPOINT: Final[str] = "SAVEPOINT cpt_run_metric_batch"
_ROLLBACK_TO_SAVEPOINT: Final[str] = "ROLLBACK TO SAVEPOINT cpt_run_metric_batch"
_RELEASE_SAVEPOINT: Final[str] = "RELEASE SAVEPOINT cpt_run_metric_batch"


def _supports_savepoints(conn: Any) -> bool:
    """这条连接值不值得用保存点保护。

    判据是 ``autocommit`` 属性**显式为真**时返回 False，其余（包括测试替身那种
    没有该属性的）返回 True。方向是刻意选的：一个不认识保存点的替身最坏结果是
    ``cur.execute("SAVEPOINT …")`` 被记进调用列表而无害（假游标不真跑 SQL），
    比反过来「以为没有保存点、于是继续谎报行数」安全得多。

    真连接上 ``psycopg`` 的 ``conn.autocommit`` 默认是 False（每条语句都在事务里），
    那时保存点才有意义 —— autocommit 下每条语句自成一个事务，先前的行**真的**
    已经落库，撤不回来，只能照实报数。
    """
    return getattr(conn, "autocommit", False) is not True


def _needs_savepoint(conn: Any, rows: Sequence[RunMetric]) -> bool:
    """这一批需不需要保存点。

    **单行批次不需要，而且这是刻意的**：只有一行时失败就是「一行都没写」，
    返回 0 本来就已经是真话，调用方自己的 rollback 足以把连接从 aborted 态救回来
    （R45 那条路径今天仍然正确）。谎报**只**出现在 2 行及以上 —— 那时前 n-1 行
    真的进过事务、又会随 aborted 事务一起消失，必须先撤掉才能诚实地报 0。

    生产上今天所有调用方都是单行（``MetricRecorder.record``、``run_inspection``），
    所以这个条件等于「热路径零开销、只给坏路径加两次往返」。
    """
    return len(rows) > 1 and _supports_savepoints(conn)


__all__ = [
    "HEALTH_VALUES",
    "KIND_INSPECTION",
    "KIND_RUN",
    "RunMetric",
    "RunMetricError",
    "append_metrics",
    "ensure_table",
    "latest_inspection",
    "latest_run_fingerprint",
    "prune",
    "recent_metrics",
    "waterline_trend",
]


class RunMetricError(RuntimeError):
    """巡检/水位表**写与清理**失败（R45 新增）。

    读路径的降级语义各不相同（见各函数 docstring），但「写不进���」与
    「清不掉」一律抛 —— 两者都是**故障**，不是业务事实。
    """


KIND_RUN: Final[str] = "run"
KIND_INSPECTION: Final[str] = "inspection"

#: 三态而不是二态 —— ``degraded`` 是"配置导致的已知降级"，``failing`` 才是要叫人起床的
HEALTH_VALUES: Final[tuple[str, ...]] = ("ok", "degraded", "failing")

_COLUMNS: Final[tuple[str, ...]] = (
    "observed_at",
    "kind",
    "market",
    "symbol",
    # --- 第二轨：可复现指纹 ---
    "config_hash",
    "dataset_hash",
    "rules_version",
    "backend",
    # --- 第二轨：结构计数 ---
    "bar_count",
    "fractal_count",
    "bi_count",
    "zhongshu_count",
    "trend_type_count",
    # --- 第一轨：水位 ---
    "last_bar_time",
    "gap_count",
    "stale",
    "factor_coverage",
    "snapshot_age_ms",
    # --- 结论 ---
    "health",
    "detail",
)

_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS public.cpt_run_metric (
    id                bigserial   PRIMARY KEY,
    observed_at       timestamptz NOT NULL DEFAULT now(),
    kind              text        NOT NULL DEFAULT 'run',
    market            text        NOT NULL DEFAULT '',
    symbol            text        NOT NULL DEFAULT '',
    config_hash       text        NOT NULL DEFAULT '',
    dataset_hash      text        NOT NULL DEFAULT '',
    rules_version     text        NOT NULL DEFAULT '',
    backend           text        NOT NULL DEFAULT '',
    bar_count         integer     NOT NULL DEFAULT 0,
    fractal_count     integer     NOT NULL DEFAULT 0,
    bi_count          integer     NOT NULL DEFAULT 0,
    zhongshu_count    integer     NOT NULL DEFAULT 0,
    trend_type_count  integer     NOT NULL DEFAULT 0,
    last_bar_time     bigint      NOT NULL DEFAULT 0,
    gap_count         integer     NOT NULL DEFAULT 0,
    stale             boolean     NOT NULL DEFAULT false,
    factor_coverage   numeric     NOT NULL DEFAULT 0,
    snapshot_age_ms   bigint      NOT NULL DEFAULT 0,
    health            text        NOT NULL DEFAULT 'ok',
    detail            jsonb       NOT NULL DEFAULT '{}'::jsonb
)
"""

_INDEX_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS cpt_run_metric_observed_idx "
    "ON public.cpt_run_metric (observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS cpt_run_metric_kind_idx "
    "ON public.cpt_run_metric (kind, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS cpt_run_metric_symbol_idx "
    "ON public.cpt_run_metric (market, symbol, observed_at DESC)",
)


#: 一行水位/指纹。**刻意是 dict 而不是 tuple** —— 20 个字段的 tuple 读代码时
#: 没人知道第 7 个是什么；键名自带语义，且前端/巡检都按键取。
#: 曾经试过 ``class RunMetric(dict)``，mypy 对 dict 子类的 ``__add__`` 重载意见很大，
#: 收益抵不上麻烦，于是退回别名。
RunMetric = dict[str, Any]


def ensure_table(conn: Any) -> None:
    """建表 + 建索引（幂等）。**不 commit** —— 由调用方决定。

    ⚠️ **本函数是 ``public.cpt_run_metric`` schema 的唯一来源。**
    ``scripts/migrations/`` 里**没有**这张表的迁移文件（其他表都有），
    所以：

    - 删掉本函数 ⇒ 表一旦被 drop / 换库，代码里**没有任何东西能重建它**；
    - 改 ``_DDL`` 的列之后，**必须在真机上执行一次**，否则代码与库会漂移
      —— 而漂移不会让任何测试变红（表是 ``CREATE TABLE IF NOT EXISTS``，
      已存在的表不会被改）。

    R45 已核实（2026-10-03 真库）：当前 ``_DDL`` 与
    ``emotion_core.public.cpt_run_metric`` **零漂移** —— 22 列、3 个索引、
    21 条 NOT NULL 约束、``kind`` 取值全部对得上。

    复核命令与「为什么还没转成迁移文件」的裁决见
    ``docs/archive/reviews-r45.md``。
    """
    with conn.cursor() as cur:
        cur.execute(_DDL)
        for stmt in _INDEX_DDL:
            cur.execute(stmt)


def append_metrics(conn: Any, rows: Sequence[RunMetric]) -> int:
    """批量写入。返回**将要随调用方 ``commit()`` 落库**的行数。

    # gate: allow-silent: 观测/记账数据是**旁路**，写失败不该让主计算路径挂。
    # 调用方 ``run_metric.RunMetricRecorder.record`` 不因此中断，
    # 且已按此契约补上 rollback（R45）。

    **写入失败只记 warning、不抛** —— 观测数据丢了不该让计算路径跟着挂。

    ## R67（审计 P2-11）：返回值曾经在 PG 上是**谎报**

    R45 把返回语义定成「已入事务的行数」（第 n 行失败就报 n-1），理由是
    「那 n-1 行已经在调用方的事务里了，报 0 会导致重跑出重复行」。这个推理在
    **PG 上不成立**：一句语句失败会让**整条事务**进 aborted 态，那 n-1 行根本
    提交不上（后续 ``commit()`` 不抛、等价于 ROLLBACK）。于是调用方读到非 0
    就以为成功 —— 既不回滚也不重试，整批**静默丢弃**。返回值越"诚实"，谎越大。

    修法：批次用 ``SAVEPOINT`` 包住。失败时 ``ROLLBACK TO SAVEPOINT`` 只撤本批，
    事务从 aborted 态恢复，调用方手上还留着它自己先前的语句（不牵累）。
    此时**确实一行都没入事务**，返回 0 才是真话，重跑也安全。

    **只对多行批次加保存点**（见 :func:`_needs_savepoint`）：单行失败本来就是
    「一行都没写」，返回值天然诚实，R45 那条 rollback 路径今天仍然正确 ——
    不必为它多付两次往返。

    autocommit 连接没有事务可保护（每条语句自成一个事务，先前的行**真的**已落库），
    那里退回老语义：成功几行就报几行，且不失败即不回滚。

    ⚠️ **值为 ``None`` 的列整列省略**（让 DB 的 ``DEFAULT`` 生效），而不是写 NULL。
    DEFAULT 只在"不写这一列"时生效，显式 NULL 照样触发 NOT NULL 违约。
    这不是 ``observed_at`` 一列的特例：本表 21 列全是 ``NOT NULL``，巡检行只填
    其中几列 —— 第一版逐列判断结果只特判了 ``observed_at``，于是巡检行一写就
    炸 ``null value in column "config_hash"``。规则统一之后就不用再逐列踩。
    （代价：本表没有可空列，所以"想写 NULL"这个语义用不上。）

    R67 备选方案（未采用）：逐行 SAVEPOINT、跳过坏行继续写。收益是坏行不连累
    兄弟行，代价是**部分成功**对调用方不可见（它只拿到一个数字，不知道哪些没写、
    也不知道该不该重试）。生产上今天所有调用方都是单行（``MetricRecorder.record``、
    ``run_inspection``），批量语义不值得为它牺牲「要么整批、要么重试」的干净边界。
    """
    if not rows:
        return 0
    #: 批次保存点。同名 SAVEPOINT 重复定义是合法的（旧的被顶掉），所以并发调用
    #: 同一连接也只会撤掉自己那批（同一连接本来就不可并发，这里只是不额外制造
    #: 名字冲突面）。
    savepoint = _needs_savepoint(conn, rows)
    written = 0
    # ⚠️ ``with conn.cursor() as cur``：原来只 ``conn.cursor()`` 不进上下文
    # 管理器，游标一路泄漏到 GC（每个 batch 一个）。
    with conn.cursor() as cur:
        if savepoint:
            cur.execute(_SAVEPOINT)
        try:
            for row in rows:
                cols: list[str] = []
                values: list[Any] = []
                placeholders: list[str] = []
                for col in _COLUMNS:
                    if row.get(col) is None:
                        continue  # 整列省略 → DEFAULT
                    cols.append(col)
                    values.append(row.get(col))
                    # 转换写在**占位符**上（``%s::jsonb``），不是列名上 ——
                    # 列名里写 ``detail::jsonb`` 是语法错误（实测 ``syntax error at or
                    # near "::"``）。第一版用 executemany 时占位符是对的，
                    # 改成逐行插入时把这件事弄丢过一次。
                    placeholders.append("%s::jsonb" if col == "detail" else "%s")
                cur.execute(
                    f"INSERT INTO public.cpt_run_metric ({', '.join(cols)})"
                    f" VALUES ({', '.join(placeholders)})",
                    values,
                )
                written += 1
        except Exception as exc:  # noqa: BLE001
            if savepoint:
                # 撤掉本批。**这一步是整条修复的支点**：不做它，事务留在 aborted 态，
                # 下面无论返回几行都是谎话（提交时整批消失）。
                #
                # 撤不回去（极少）也照样报 0 —— 那时事务仍是 aborted，返回非 0 会让
                # 调用方跳过 rollback，反而把「静默丢弃」重现一遍。报 0 让调用方
                # 走它的 rollback 兜底，与下面的告警一起形成 fail-closed。
                try:
                    cur.execute(_ROLLBACK_TO_SAVEPOINT)
                except Exception as undo_exc:  # noqa: BLE001 — 兜底失败只能记录
                    _LOG.warning(
                        "cpt_run_metric 批次回滚失败（事务可能仍 aborted）：%s: %s",
                        type(undo_exc).__name__,
                        undo_exc,
                    )
            # 返回值必须**诚实**。R67 前这里报 ``written``（「前 n-1 行已入事务」），
            # 但 PG 上那 n-1 行随 aborted 事务一起没了 ⇒ 谎报 ⇒ 调用方以为成功。
            # 现在：有保存点 ⇒ 真的一行都没留下 ⇒ 0；没有保存点 ⇒ 只有两种情形，
            # 要么单行（written 必为 0 或根本没有失败行）要么 autocommit
            # （先前的行各自已落库、撤不回来），都照实报 written 才是真话。
            _LOG.warning(
                "cpt_run_metric 写入中断（本批 %d/%d 行，事务状态：%s）：%s: %s",
                written,
                len(rows),
                "已回退到保存点" if savepoint else "无保存点，已入事务的行保留",
                type(exc).__name__,
                exc,
            )
            return 0 if savepoint else written
        if savepoint:
            # 批次成功 ⇒ 释放保存点。不释放也能工作（下一个同名 SAVEPOINT 会顶掉它），
            # 但长事务里保存点会一直累积（PG 不自动回收），所以显式收尾。
            cur.execute(_RELEASE_SAVEPOINT)
    return written


def _row_to_dict(row: Sequence[Any]) -> RunMetric:
    out = RunMetric()
    for name, value in zip(_COLUMNS, row, strict=False):
        if name == "observed_at" and hasattr(value, "timestamp"):
            value = int(value.timestamp() * 1000)
        if name in {"factor_coverage"} and value is not None:
            value = float(value)
        out[name] = value
    return out


_SELECT: Final[str] = ", ".join(_COLUMNS)


def recent_metrics(
    conn: Any,
    *,
    kind: str | None = None,
    market: str | None = None,
    symbol: str | None = None,
    limit: int = 200,
) -> tuple[RunMetric, ...]:
    """按 ``observed_at`` 倒序读最近的行（**只读**，游标不出这一层）。"""
    capped = max(1, min(int(limit), 1000))
    where: list[str] = []
    args: list[Any] = []
    if kind:
        where.append("kind = %s")
        args.append(kind)
    if market:
        where.append("market = %s")
        args.append(market)
    if symbol:
        where.append("symbol = %s")
        args.append(symbol)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(capped)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SELECT} FROM public.cpt_run_metric {clause} "
            f"ORDER BY observed_at DESC LIMIT %s",
            args,
        )
        return tuple(_row_to_dict(r) for r in cur.fetchall())


def latest_inspection(conn: Any) -> RunMetric | None:
    """最近一条巡检结论（没有则 ``None``）。"""
    rows = recent_metrics(conn, kind=KIND_INSPECTION, limit=1)
    return rows[0] if rows else None


#: 参与「变化原因归因」的四个指纹字段，顺序即 :func:`explain_cause` 的判定顺序。
#: 只认 ``run`` 行 —— ``inspection`` 行是**读**水位表的结论，它自己的指纹是抄来的，
#: 拿它当「上一轮」会把归因指向一次观测而不是一次运行。
FINGERPRINT_FIELDS: Final[tuple[str, ...]] = (
    "config_hash",
    "dataset_hash",
    "rules_version",
    "backend",
)


def latest_run_fingerprint(conn: Any, *, market: str, symbol: str) -> dict[str, str] | None:
    """某标的**最近一次运行**的算法指纹（``None`` = 从没跑过）。

    ## 谁在用它、为什么时序上是对的

    :func:`cpt.application.structure_event_recorder.record_structure_events` 在写
    结构事件的那一刻调用它。那一刻**本轮的水位行还没落**（水位在一轮的最后才记），
    所以「最近一次运行」拿到的必然是**上一轮**—— 正是 ``explain_cause`` 需要的前值。

    ## 为什么返回 ``None`` 而不是空 dict

    「从没跑过」和「跑过但四个指纹都是空串」必须能区分：前者没有前值可比，
    归因应当留空（:func:`explain_cause` 返回 ``""``）；后者是真的四项全空。
    混成空 dict 会让首次运行被归到 ``code``，凭空指控算法。

    :raises RunMetricError: **读失败**。这里原来 catch 住异常返回 ``None``，
        与「确实没有上一轮」完全同值 —— 那正是上面这段要避免的事：
        库读不到被冒充成「这个标的第一轮跑」，于是 :func:`explain_cause`
        拿不到前值、归因留空，而连接还停在 aborted 态连累后面的查询。
        本模块其余读（:func:`recent_metrics` / :func:`prune`）都不吞，
        原来那句注释「与本模块其余读一致」是**假的**。
        调用方 ``structure_event_recorder`` 整段包在 try/except 里且按「归因
        失败就原样返回事件」处置，所以抛是安全的。
    """
    try:
        rows = recent_metrics(conn, kind=KIND_RUN, market=market, symbol=symbol, limit=1)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("读取上一轮指纹失败 %s/%s: %s", market, symbol, exc)
        raise RunMetricError(f"读取上一轮指纹失败 {market}/{symbol}: {exc}") from exc
    if not rows:
        return None
    row = rows[0]

    # ⚠️ ``recent_metrics`` 的元素是 **dict**（``_row_to_dict`` 的返回值），
    # 不是 ``RunMetric`` 实例 —— 用 ``getattr`` 取会静默拿到 ``""``，
    # 于是「上一轮指纹全空」→ 每一轮都被归成 ``backend``。
    # 这是真机上逮到的：库里那行四个字段明明有值，读出来却是空的。
    def _field(name: str) -> str:
        if isinstance(row, dict):
            return str(row.get(name) or "")
        return str(getattr(row, name, "") or "")

    return {name: _field(name) for name in FINGERPRINT_FIELDS}


#: 参与「趋势变化」判定的字段，顺序即 :func:`_trend_from_rows` 的 diff 顺序。
_TREND_TRACKED_FIELDS: Final[tuple[str, ...]] = (
    "bar_count",
    "bi_count",
    "zhongshu_count",
    "gap_count",
    "factor_coverage",
    "last_bar_time",
    "health",
    "backend",
    "dataset_hash",
)

#: 带表别名的列清单，只给 :func:`waterline_trends_bulk` 的 LATERAL 子查询用。
#: ``_SELECT`` 不带别名，而子查询里 ``market``/``symbol`` 同时属于键表和
#: 水位表，不加限定会直接 ``column reference is ambiguous``。
_SELECT_QUALIFIED: Final[str] = ", ".join(f"m.{name}" for name in _COLUMNS)


def _trend_from_rows(market: str, symbol: str, rows: Sequence[RunMetric]) -> dict[str, Any]:
    """把**按时间升序**的水位行压成趋势；空序列返回零样本形态。"""
    if not rows:
        return {"market": market, "symbol": symbol, "samples": 0, "changes": []}
    changes: list[dict[str, Any]] = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        diff = {
            k: [prev.get(k), cur.get(k)] for k in _TREND_TRACKED_FIELDS if prev.get(k) != cur.get(k)
        }
        if diff:
            changes.append({"at": cur.get("observed_at"), "changed": diff})
    return {
        "market": market,
        "symbol": symbol,
        "samples": len(rows),
        "first_seen": rows[0].get("observed_at"),
        "last_seen": rows[-1].get("observed_at"),
        "latest": rows[-1],
        "changes": changes[-20:],
    }


def waterline_trend(conn: Any, *, market: str, symbol: str, limit: int = 50) -> dict[str, Any]:
    """把某标的的水位序列压成"趋势"（给前端/巡检看的极简形态）。

    只给**变化了的**字段 —— 一串 50 行全等的水位对人是噪声。
    """
    rows = tuple(reversed(recent_metrics(conn, market=market, symbol=symbol, limit=limit)))
    return _trend_from_rows(market, symbol, rows)


def waterline_trends_bulk(
    conn: Any,
    keys: Sequence[tuple[str, str]],
    limit: int = 50,
) -> dict[tuple[str, str], dict[str, Any]]:
    """一次查询算出多个 ``(market, symbol)`` 的水位趋势（**只读**）。

    ## R59（审计 M5）：为什么要有它

    ``/api/dashboard/inspection`` 原先对每个 distinct 标的调一次
    :func:`waterline_trend`，上限 20 个 key 就是 20 次往返（N+1）。
    这里用一条 ``LATERAL`` 语句按 key 各取 ``limit`` 行，语义与逐个调用
    **完全一致**（等价性由 ``tests/test_run_metric_store_trends_bulk.py`` 钉住）。

    返回值为 ``{(market, symbol): trend}``；**每个请求到的 key 都必有条目**
    （没数据的 key 是零样本形态），顺序不影响结果，重复 key 会先去重。

    :param keys: ``(market, symbol)`` 序列；空序列直接返回空 dict（不发 SQL）。
    :param limit: 每个 key 各取最近多少行，与 :func:`waterline_trend` 同口径。
    """
    capped = max(1, min(int(limit), 1000))
    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for market, symbol in keys:
        key = (str(market), str(symbol))
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    empty = {key: _trend_from_rows(key[0], key[1], ()) for key in unique}
    if not unique:
        return empty

    shape = ", ".join(["(%s::text, %s::text)"] * len(unique))
    args: list[Any] = [value for key in unique for value in key]
    args.append(capped)
    sql = (
        f"SELECT {_SELECT} "
        f"FROM (VALUES {shape}) AS k(k_market, k_symbol) "
        f"CROSS JOIN LATERAL ("
        f"SELECT {_SELECT_QUALIFIED} FROM public.cpt_run_metric m "
        f"WHERE m.market = k.k_market AND m.symbol = k.k_symbol "
        f"ORDER BY m.observed_at DESC LIMIT %s"
        f") AS t "
        f"ORDER BY t.market, t.symbol, t.observed_at DESC"
    )

    grouped: dict[tuple[str, str], list[RunMetric]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, args)
        for raw in cur.fetchall():
            metric = _row_to_dict(raw)
            key = (str(metric.get("market") or ""), str(metric.get("symbol") or ""))
            if key not in empty:
                continue
            grouped.setdefault(key, []).append(metric)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key in unique:
        rows = tuple(reversed(grouped.get(key, [])))
        out[key] = _trend_from_rows(key[0], key[1], rows)
    return out


def prune(conn: Any, *, keep_days: int = 30, kinds: Sequence[str] | None = None) -> int:
    """删掉 ``keep_days`` 之前的行，返回删除行数。**不 commit**。

    run 行是高频的（每轮一行），不留窗口就会长成第二份 ``daily_bar``。

    ## R45：加 ``kinds`` 过滤

    原来**不带任何 kind 条件**，一调用就把 ``inspection`` 行一起删了 ——
    而巡检行正是「每天状态变化比对」的依据，窗口该比 run 行短得多，
    混在一起按同一个窗口删会误伤。

    2026-10-03 实测该表两类行：

        run          3234 行  2026-10-02 ~ 2026-10-03
        inspection      9 行  2026-10-02 ~ 2026-10-03

    所以保留策略要能分开配：``kinds=[KIND_RUN]`` + 90 天，
    ``kinds=[KIND_INSPECTION]`` + 更短的窗口。
    ``kinds=None`` 保持原行为（全删），不破坏既有调用方。

    ## R45：失败**抛**，不返回 0

    原来失败时 ``return 0``，与「本来就没有过期行」**完全同值** ——
    于是定时清理作业无法判断自己是「干完了」还是「一条都没删掉」，
    慢性泄漏就会在日志里静悄悄地继续。

    改成抛之后，调用方（``deploy/cron/run-metric-prune-daily.sh``）
    catch → 打 ``!!!!! 清理未完成 !!!!!`` → **非零码退出**，
    失败才真的可见。这与本层其他函数、以及 R45 建立的
    「业务事实可以返回，**故障必须抛**」口径一致。
    """
    params: list[Any] = [max(1, int(keep_days))]
    where = "observed_at < now() - make_interval(days => %s)"
    if kinds:
        wanted = [str(k) for k in kinds]
        where += " AND kind = ANY(%s)"
        params.append(wanted)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM public.cpt_run_metric WHERE {where}", tuple(params))
            return int(cur.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("cpt_run_metric 清理失败：%s: %s", type(exc).__name__, exc)
        raise RunMetricError(f"cpt_run_metric 清理失败: {exc}") from exc
