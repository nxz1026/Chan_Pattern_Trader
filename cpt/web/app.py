"""Minimal standard-library HTTP adapter for a supplied Dashboard snapshot."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlencode, urlsplit

from cpt.adapters.binance_futures import resolve_interval_label
from cpt.application.dashboard_compare import compare_snapshots
from cpt.application.dashboard_export import slice_snapshot
from cpt.application.dashboard_levels import level_tree
from cpt.application.dashboard_multi_run import align_runs
from cpt.application.dashboard_runs import find_run, recent_runs, record_run, run_body
from cpt.application.dashboard_stats import signal_statistics
from cpt.application.dashboard_watchlist import watchlist_rows
from cpt.web.rate_limit import SlidingWindowLimiter, client_ip

_LOG = logging.getLogger("cpt.web.handler")

#: R59（审计 H1）：LLM 端点的默认配额 —— 同一「来源 + 端点」60s 内最多 10 次。
#:
#: 为什么是 10：正常人一次会话点几下「讲人话 / 解释结构」，10 次/分钟远超手速；
#: 而脚本循环一分钟能烧掉的额度正好被钉死在 10 次。被拒回 429 + ``Retry-After``。
_DEFAULT_LLM_MAX_EVENTS = 10
_DEFAULT_LLM_WINDOW_S = 60.0

#: R59（审计 H3）：付费探活（Wind 额度）的默认配额 —— 同一来源 300s 内最多 3 次。
#:
#: 为什么不是 10：这里的单位是**钱**（一次探测一次真实额度），而它本来只是排障用的
#: 一次性检查，3 次/5 分钟足够；比 LLM 更紧，因为失败代价不可逆（额度已花）。
_DEFAULT_QUOTA_MAX_EVENTS = 3
_DEFAULT_QUOTA_WINDOW_S = 300.0

#: R59（审计 H2）：加密侧 ``?symbol=`` 的白名单形状（Binance 现货/合约交易对，如 ``BTCUSDT``）。
#:
#: 为什么用正则而不是「问 provider 认不认识」：``select_symbol`` 的下游会在 HTTP
#: 工作线程里**同步重算整条流水线并写库**，非法 symbol 走到那里就已经付出代价了。
#: 先在入口把形状挡掉，不合法**绝不落到上游**。
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")

#: R59（审计 H4）：TCP 并发上限。
#:
#: 原实现是裸 ``ThreadingHTTPServer`` —— 来多少连就开多少线程，一个慢下游
#: （LLM/数据库）就能把线程数顶到几百。超限的连接直接回 503，而不是排队到超时。
_DEFAULT_MAX_WORKERS = 32

#: R59（审计 M5）：分页/窗口参数的统一上界。
#:
#: 原实现只做 ``int()``，``?limit=100000000`` 会让每个 (market, symbol) 都去
#: 拉一次 ``waterline_trend``（N+1 放大）。与 A 股 ``width_k`` 的 5..2000 同类，
#: 这里把「一次响应能承载多少条」钉成有界区间。
_MAX_LIMIT = 500
_MAX_DAYS = 3650

#: R59（审计 M5）：``/inspection`` 里 distinct ``(market, symbol)`` 的硬上限。
#:
#: 每个 key 一次 ``waterline_trend``（N+1），而 key 数随 run 行数无限增长。
#: 取 20：看板同时盯的标的（加密 1~3 个 + A 股几只）远不到这个量级，
#: 正常巡检**看不见**这个上限；真撞上说明数据被灌脏了，那时宁可标注截断
#: （响应的 ``limits.trend_keys_truncated``）也不要把 DB 打满。
#: 真正的修法是一条批量查询 ``waterline_trends_bulk(conn, keys, limit)``，
#: 那要改 ``cpt/storage``（不在本批范围，已在回报里列出所需接口）。
_MAX_TREND_KEYS = 20

#: access log 里**从不落盘**的查询参数（R59／审计 H4）。
#:
#: 令牌/回调地址一旦进日志就等于泄露 —— 日志会被打包、会被转发。命中的键只留键名，
#: 值替换成 ``***``；其余参数照常记录，排障信息不受影响。
_SENSITIVE_QUERY_KEYS = frozenset(
    {"token", "access_token", "api_key", "apikey", "key", "secret", "password", "webhook", "hook"}
)

SnapshotProvider = Callable[[], dict[str, Any]]


class SnapshotSource(Protocol):
    """Read-only provider exposing a ``snapshot_payload`` method."""

    def snapshot_payload(self) -> dict[str, Any]: ...


@runtime_checkable
class MultiLevelSource(Protocol):
    """Read-only provider that can rebuild a snapshot for a given level."""

    def snapshot_for_level(self, level: int) -> dict[str, Any]: ...


@runtime_checkable
class InspectProvider(Protocol):
    """Read-only per-bar inspection contract for ``/api/dashboard/inspect``."""

    def inspect(self, bar_index: int) -> dict[str, Any]: ...


@runtime_checkable
class RangeSource(Protocol):
    """Read-only provider that can rebuild a snapshot for an explicit time window."""

    def snapshot_for_range(self, start_ms: int, end_ms: int) -> dict[str, Any]: ...


@runtime_checkable
class SelectableSource(Protocol):
    """Provider that supports hot-reloading the live symbol/interval.

    Implemented by ``_RealtimeProvider`` so the HTTP layer can route
    ``?symbol=`` / ``?interval_ms=`` requests to the live data stream
    rather than the snapshot's cached market.symbol field only.
    """

    def select_symbol(self, symbol: str, interval: str) -> None: ...

    def force_refresh(self) -> None: ...


def _redact_path_for_log(path_with_query: str) -> str:
    """把请求行里的查询串脱敏后返回（R59／审计 H4）。

    只动**键名命中** :data:`_SENSITIVE_QUERY_KEYS` 的值：键名保留（排障时知道
    「带了 token」），值一律换 ``***``。其余参数原样——access log 的价值就在于
    能看出请求了什么。

    被拒方案：整条 query 一律不记。那样的 access log 无法定位「谁在刷
    ``?include_quota=1``」，而这正是 H1/H3 要观测的行为；只遮敏感键更实用。
    """
    split = urlsplit(path_with_query)
    if not split.query:
        return split.path
    try:
        pairs = parse_qs(split.query, keep_blank_values=True)
    except ValueError:  # pragma: no cover - parse_qs 对 str 不抛，防御性
        return split.path
    redacted: dict[str, list[str]] = {
        key: ["***"] * len(values) if key.lower() in _SENSITIVE_QUERY_KEYS else values
        for key, values in pairs.items()
    }
    return f"{split.path}?{urlencode(redacted, doseq=True)}"


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` + **有界线程**（R59／审计 H4）。

    原实现来一个连接开一条线程：慢下游（LLM / DB）叠加并发请求就能把线程数顶到
    几百，进程内存与下游连接池一起被打爆。这里用一个信号量把**同时在跑的请求**
    限制在 ``max_workers``；拿不到名额的连接当场回 503 + ``Retry-After``，
    **不排队**（排队只会把超时推给客户端，还占着 socket）。

    Attributes:
        max_workers: 并发处理上限（``<= 0`` 表示不设限，供测试/兼容旧行为）。
        rejected_connections: 被 503 拒掉的累计连接数（运维可观测，不落盘）。

    关闭语义（审计点名的坑）：``daemon_threads=True`` 让工作线程不阻塞进程退出；
    ``block_on_close=False`` 让 ``server_close()`` **不 join** 工作线程 ——
    否则一条卡在 LLM 上的请求会让关停一直挂到超时。
    """

    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        max_workers: int = _DEFAULT_MAX_WORKERS,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.max_workers = max_workers
        self.rejected_connections = 0
        self._slots = threading.BoundedSemaphore(max_workers) if max_workers > 0 else None

    def process_request(self, request: Any, client_address: Any) -> None:
        slots = self._slots
        if slots is None:
            super().process_request(request, client_address)
            return
        if not slots.acquire(blocking=False):
            self.rejected_connections += 1
            self._reject_overloaded(request, client_address)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # 起线程失败必须把名额还回去，否则每失败一次就永久少一个名额。
            slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            slots = self._slots
            if slots is not None:
                slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """R59（2026-10-09 日志巡检 B）：客户端提前断连不该刷整段 Traceback。

        ``socketserver`` 处理请求时抛出的任何异常都会走这里，默认实现用
        ``traceback.print_exc()`` 往 stderr/journal 打一份完整栈。浏览器关标签页、
        超时取消是**常态**，7 天 journal 里 CPT 由此留下 8 段 Traceback，真正的
        故障信号被淹没。断连只记 DEBUG；其余异常仍交回默认实现（不吞真错）。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            _LOG.debug("客户端断连 %s: %s", client_address, type(exc).__name__)
            return
        super().handle_error(request, client_address)

    def _reject_overloaded(self, request: Any, client_address: Any) -> None:
        """在 accept 线程里直接回 503（不经 handler，故手写状态行）。"""
        body = json.dumps(
            {"error": {"code": "server_busy", "message": "并发请求过多，请稍后重试"}},
            ensure_ascii=False,
        ).encode("utf-8")
        head = (
            "HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Retry-After: 1\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        peer = client_address[0] if client_address else "-"
        _LOG.warning("并发上限 %d 已满，拒绝连接 %s", self.max_workers, peer)
        try:
            request.sendall(head + body)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)


#: ``/api/dashboard/runs`` 面板一次拉多少行。ring 是 50，表侧同量级。
_RUN_INDEX_LIMIT = 50


@contextmanager
def _run_store_conn(conn: Any = None) -> Iterator[Any]:
    """借一条 psycopg 连接给运行持久化表用。

    :param conn: 已有的连接（测试注入）。给了就直接用，**不负责关闭**。

    没给就现开一条并在退出时关掉——复用 ``_signal_stats_payload`` 那套
    ``AShareLocalClient`` 借连接的写法。psycopg 与 ``dashboard_run_store``
    都**惰性导入**：CI 只跑 ``pip install -e .``（不带 ``[db]``），顶层拖进来
    会让整包测试在 collection 阶段就炸。
    """
    if conn is not None:
        yield conn
        return
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415

    client = AShareLocalClient()
    try:
        yield client._get_conn()  # noqa: SLF001
    finally:
        client.close()


def _persist_run(row: dict[str, Any], body: dict[str, Any] | None) -> None:
    """``record_run`` 的 PG 双写旁路（R23）。

    **best-effort，异常一律吞掉**：记一次账失败不该让用户的 HTTP 响应 500。
    失败的真实后果只是「这次运行重启后查不到」，日志里留痕即可——这与
    ``dashboard_runs`` 的 ring 是完全独立的两条路径。
    """
    # ⚠️ R45 新增开关 ``CPT_RUN_STORE_PERSIST=0``：**测试用**。
    #
    # 实测踩到：web 契约测试起真 server，会真的往**生产表** ``cpt_dashboard_run``
    # 写行（实测已积 89 行，2026-10-01 → 10-04）。而 ``/api/dashboard/runs``
    # 是「**表优先**」⇒ 测试读回的是**别的测试写的行**，不是自己的 ring ——
    # 于是 ``test_timestamp_falls_back_when_runtime_omits_generated_at``
    # 长期失败（拿到的 generated_at 是别的测试的挂钟时间，不是本例的 as_of_ms）。
    #
    # ⇒ 三个问题一起解决：
    #   1. 测试不再污染生产表；
    #   2. 读路由回落 ring ⇒ 每个测试的断言**只看自己**，可复现；
    #   3. **生产默认行为不变**（不设该变量时照旧双写）。
    import os as _os  # noqa: PLC0415

    if _os.environ.get("CPT_RUN_STORE_PERSIST", "1") == "0":
        return

    try:
        from cpt.storage.dashboard_run_store import upsert_run  # noqa: PLC0415

        with _run_store_conn() as conn:
            upsert_run(conn, row, body)
            # 事务边界归调用方——与 ``a_share_snapshot.py:352`` 调完
            # ``record_signal_event`` 紧接着 ``conn.commit()`` 同一套路。
            #
            # **这一行漏了会静默丢数据**：``AShareLocalClient`` 走的是裸
            # ``psycopg.connect()``（无 autocommit），退出 with 时
            # ``client.close()`` 会把未提交的 INSERT **回滚**。实测踩过：
            # oracle 上 HTTP 全部 200、journalctl 里一条告警都没有、
            # ``cpt_dashboard_run`` 却是 0 行——因为 ``upsert_run`` 正常返回、
            # 不抛异常，异常分支根本没被触发。store 层不 commit 是**故意**的
            # （与 R21 一致，便于多个写入共享一个事务），所以提交义务在这里。
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — 旁路记账，绝不允许反噬主流程
        _LOG.warning("运行持久化双写失败（不影响本次响应）: %s", exc)


def _load_run_bodies(run_ids: list[str], conn: Any = None) -> dict[str, dict[str, Any] | None]:
    """从表里批量取运行本体；表不可用时返回 ``{}``（调用方回落 ring）。

    返回值的「key 在 / key 不在」语义见
    :func:`cpt.storage.dashboard_run_store.get_snapshots`：key 在但值是
    ``None`` 表示**库里明确记了没有本体**（4MB 闸门），不能回落 ring 把它掩盖掉。
    """
    if not run_ids:
        return {}
    try:
        from cpt.storage.dashboard_run_store import get_snapshots  # noqa: PLC0415

        with _run_store_conn(conn) as opened:
            return get_snapshots(opened, run_ids)
    except Exception as exc:  # noqa: BLE001 — 表是增强，回落 ring 即可
        _LOG.warning("运行本体表不可用，回落 in-process ring: %s", exc)
        return {}


def _index_row_from_body(run_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
    """从快照本体反推一条最小索引行（``run_id`` + ``dataset_hash``）。

    跨重启比较时 ring 是空的，``find_run`` 必然返回 ``None``，``dataset_hash``
    就回填不上——而它是 ``dashboard_compare`` 差异摘要里的口径字段。正好本体
    就是完整的 snapshot，``reproducibility.dataset_hash`` 直接可取，**不额外
    打一次 DB**。
    """
    if not isinstance(body, dict):
        return {"run_id": run_id}
    reproducibility = body.get("reproducibility")
    dataset_hash = (
        reproducibility.get("dataset_hash") if isinstance(reproducibility, dict) else None
    )
    row: dict[str, Any] = {"run_id": run_id}
    if dataset_hash is not None:
        row["dataset_hash"] = dataset_hash
    return row


def _run_sort_key(row: dict[str, Any]) -> float:
    """运行索引的排序键：``generated_at`` 降序，缺失/脏值沉底。

    表侧出的是 int 毫秒（``_row_to_run_index`` 统一转过），ring 侧是
    ``build_run_index`` 透传的原始值，可能是 float、ISO 串甚至 ``None``。
    混在一起直接比较会 ``TypeError`` 把整页打成 500，所以这里**全部降成
    float**，转不动的给 ``-inf`` 沉到最后。
    """
    value = row.get("generated_at")
    if isinstance(value, bool) or value is None:
        return float("-inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def _run_index_rows(limit: int = _RUN_INDEX_LIMIT, conn: Any = None) -> list[dict[str, Any]]:
    """``/api/dashboard/runs`` 的数据源：**表 + ring 合并**，按 ``run_id`` 去重。

    为什么不是「表优先 → ring 兜底」那种二选一：二选一在**部分写失败**时会
    丢数据。双写是 best-effort 的（``_persist_run`` 吞异常），所以完全可能出
    现「表里有昨天重启前写的 3 行，本进程这轮新写的 5 行因为权限/连接问题
    一行没进去」—— 此时纯读表会**看不见最新的 5 行**，比 R20 还差。

    合并规则：

    1. **表优先**：同一个 ``run_id`` 两边都有时用表里那份（它是已提交的
       持久态，还多带一个 ``body_recorded``）。
    2. **ring 补缺**：只把表里没有的 ``run_id`` 加进来。
    3. 合并后按 ``generated_at`` 降序，截 ``limit``。

    表**完全不可用**（连不上/表不存在）时退化成纯 ring —— 即 R20 行为。
    """
    # ⚠️ R45：与 ``_persist_run`` 同一个开关，**读端也要关**。
    # 只关写端不够 —— 表里**还留着**历史写进去的行（实测 89 行），
    # 而本函数是「表优先」⇒ 测试读回的是那些行的挂钟时间，
    # 而不是本例的 ``as_of_ms``。⇒ 开关管住读写两端，测试才只看自己。
    import os as _os  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if _os.environ.get("CPT_RUN_STORE_PERSIST", "1") == "0":
        # 读端也关 ⇒ 纯 ring（R20 行为），完全由本次进程决定，可复现。
        for row in recent_runs(limit):
            rows.append(dict(row))
        rows.sort(key=_run_sort_key, reverse=True)
        return rows[:limit]
    try:
        # 别名导入：裸 ``recent_runs`` 会遮蔽模块级那个（in-process ring 版），
        # 兜底分支就会把 ring 的 limit 参数当 conn 传进去。
        from cpt.storage.dashboard_run_store import (  # noqa: PLC0415
            recent_runs as store_recent_runs,
        )

        with _run_store_conn(conn) as opened:
            for row in store_recent_runs(opened, limit):
                rows.append(dict(row))
                if row.get("run_id"):
                    seen.add(str(row["run_id"]))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("运行索引表不可用，退化为 in-process ring: %s", exc)

    for row in recent_runs(limit):
        run_id = str(row.get("run_id") or "")
        if run_id and run_id in seen:
            continue
        rows.append(dict(row))

    rows.sort(key=_run_sort_key, reverse=True)
    return rows[:limit]


def _with_run_index(payload: dict[str, Any]) -> dict[str, Any]:
    """在 **HTTP 响应层** 注入运行索引（R20 接线，R23 追加 PG 双写）。

    为什么不在 ``build_dashboard_snapshot_v2`` 里接：运行历史是**进程级状态**，
    放进去会让 snapshot 变成非确定性的 —— 同参数两次调用返回不等，直接打破
    ``tests/test_web_a_share.py::test_provider_caches_snapshot_within_ttl`` 守的
    缓存语义，也会让 ``dashboard_compare`` 的字段级 diff 永远有一处差异
    （``runs``）。这是接线过程中实测踩到的，不是推演。

    只有含 ``market`` 的完整 snapshot 才注入；``/api/dashboard/reproducibility``
    这类子字段响应保持原样。

    ``record_run`` 自带去重：realtime 模式 30s 内可能有十几次请求命中同一份
    缓存 snapshot，不去重会把「一次运行」记成十几条。去重命中时 ``record_run``
    **不触发** ``on_recorded``，所以那十几次请求也不会打十几次 DB。
    """
    if "market" not in payload:
        return payload
    record_run(payload, on_recorded=_persist_run)
    enriched = dict(payload)
    enriched["runs"] = list(recent_runs())
    return enriched


#: diff 摘要里的内容指纹长度（hex 字符）。12 位足够区分，又不会把响应撑大。
_DIFF_HASH_CHARS = 12


def _diff_digest(value: Any) -> str:
    """序列内容指纹：同内容同摘要、跨进程稳定（内置 ``hash`` 带随机种子，不能用）。"""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:_DIFF_HASH_CHARS]


def _summarize_diff_value(name: str, value: Any) -> Any:
    """把 diff 里的序列型值降级成 ``{"__summary__": ..., "count": ..., "hash": ...}``。

    ``snapshot_diff`` 是**顶层**字段级 diff，而 ``overlays`` 是「dict 里套 4 个 list」——
    只判顶层是不是 list 会漏掉它，一份快照几百根 K 线的原始数组正是从这个口子漏进
    响应的（前端 ``JSON.stringify`` 会渲染成几兆巨串），所以这里必须递归。
    """
    if isinstance(value, (list, tuple)):
        return {"__summary__": name, "count": len(value), "hash": _diff_digest(value)}
    if isinstance(value, dict):
        return {key: _summarize_diff_value(f"{name}.{key}", item) for key, item in value.items()}
    return value


def _summarize_diff_entries(entries: Any) -> list[dict[str, Any]]:
    """把 ``snapshot_diff`` 的每条差异摘要化（理由见 :func:`_summarize_diff_value`）。"""
    summarized: list[dict[str, Any]] = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("field"))
        summarized.append(
            {
                "field": entry.get("field"),
                "left": _summarize_diff_value(name, entry.get("left")),
                "right": _summarize_diff_value(name, entry.get("right")),
            }
        )
    return summarized


def _signal_stats_payload(days: int, code: str | None) -> dict[str, Any]:
    """C5 信号事件统计。**降级不抛**：psycopg 缺失 / DB 不可达 / 表不存在都回 available=false。

    ``basis`` 是刻意自报的口径：这批统计是**信号状态跃迁事件**的分布，不是「当前若干
    只票的状态快照」。不自报的话前端只能猜，而两者数值完全不同。
    """
    payload: dict[str, Any] = {
        "schema_version": "dashboard_signal_stats.v1",
        "basis": "signal_event_transitions",
        "days": days,
    }
    try:
        # CI 只跑 ``pip install -e .``（不带 [db]），故连接层必须惰性导入。
        from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
        from cpt.storage.signal_event_store import load_signal_events  # noqa: PLC0415

        client = AShareLocalClient()
        try:
            events = load_signal_events(client._get_conn(), days=days, code=code)  # noqa: SLF001
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — 只读增强，DB 抖动不该让主视图 500
        _LOG.warning("signal stats unavailable: %s", exc)
        payload["available"] = False
        payload["reason"] = "signal_history_unavailable"
        return payload
    payload["available"] = True
    payload["stats"] = signal_statistics(list(events))
    return payload


def _structure_events_payload(
    *,
    limit: int,
    event_type: str | None,
    kind: str | None,
) -> dict[str, Any]:
    """R27：最近结构事件流。**降级不抛**，与 :func:`_signal_stats_payload` 同纪律。

    ``basis`` 刻意自报口径：这批是 ``cpt_structure_event`` 里**发生过的事件**
    （append-only 累积），不是「当前有多少个结构」。前端若拿它当状态快照用就会
    得出「结构有 725 个」这种结论 —— 真实含义是「累计发生过 725 次变化」。
    """
    payload: dict[str, Any] = {
        "schema_version": "dashboard_structure_events.v1",
        "basis": "structure_event_stream",
        "limit": limit,
        "event_type": event_type,
        "kind": kind,
    }
    try:
        # CI 只跑 ``pip install -e .``（不带 [db]），故连接层必须惰性导入。
        from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
        from cpt.storage.structure_event_store import recent_events  # noqa: PLC0415

        client = AShareLocalClient()
        try:
            events = recent_events(
                client._get_conn(),  # noqa: SLF001
                limit=limit,
                event_type=event_type,
                kind=kind,
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — 只读旁路，DB 抖动不该让主视图 500
        _LOG.warning("structure events unavailable: %s", exc)
        payload["available"] = False
        payload["reason"] = "structure_event_stream_unavailable"
        payload["count"] = 0
        payload["events"] = []
        return payload
    payload["available"] = True
    payload["count"] = len(events)
    payload["events"] = [dict(event) for event in events]
    return payload


def _structure_timeline_payload(structure_id: str, *, limit: int) -> dict[str, Any]:
    """R27：单个结构的事件时间线（revision 升序）。**降级不抛**。"""
    payload: dict[str, Any] = {
        "schema_version": "dashboard_structure_timeline.v1",
        "basis": "structure_event_stream",
        "structure_id": structure_id,
        "limit": limit,
    }
    try:
        from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
        from cpt.storage.structure_event_store import timeline  # noqa: PLC0415

        client = AShareLocalClient()
        try:
            events = timeline(
                client._get_conn(),  # noqa: SLF001
                structure_id,
                limit=limit,
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("structure timeline unavailable: %s", exc)
        payload["available"] = False
        payload["reason"] = "structure_event_stream_unavailable"
        payload["count"] = 0
        payload["events"] = []
        return payload
    payload["available"] = True
    payload["count"] = len(events)
    payload["events"] = [dict(event) for event in events]
    return payload


def _latest_signal_statuses() -> dict[str, str]:
    """各 code 的最新信号状态；事件流不可用 → 空表（调用方补 ``"none"``）。

    返回空 dict 是**诚实**的缺省：``watchlist_rows`` 的 ``signal_status`` 缺省本就是
    ``"none"``，所以「没读到」不会伪装成别的结论，只是这一列失去信息量。
    """
    try:
        from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
        from cpt.storage.signal_event_store import load_signal_events  # noqa: PLC0415

        client = AShareLocalClient()
        try:
            events = load_signal_events(client._get_conn(), days=30)  # noqa: SLF001
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("watchlist signal statuses unavailable: %s", exc)
        return {}
    latest: dict[str, str] = {}
    for event in events:  # 已按 transition_time 倒序 → 首次出现即最新
        event_code = event.get("code")
        if isinstance(event_code, str) and event_code not in latest:
            latest[event_code] = str(event.get("status", "none"))
    return latest


def _watchlist_payload() -> dict[str, Any]:
    """C6 候选池 → ``watchlist_rows`` 入参形状。**只做映射**，不新增计算口径。

    价格/涨跌幅来自本地库最近两根收盘价，信号状态来自信号事件流最新一条。任一环取不到
    就填诚实的缺省值（``None`` / ``"none"``），而不是 500 —— 一只票取不到价不该把整个
    列表搞挂。
    """
    from cpt.web import a_share_routes  # noqa: PLC0415 — 避免顶层拖入 psycopg

    schema = {"schema_version": "dashboard_watchlist.v1"}
    try:
        pool = a_share_routes.pool_payload()
    except Exception as exc:  # noqa: BLE001 — 池子不可用是正常降级
        _LOG.warning("watchlist pool unavailable: %s", exc)
        return {**schema, "available": False, "reason": "pool_unavailable"}
    items = pool.get("items") or []
    # 池子全空 + 记了 db_error ⇒ 是「库连不上」而不是「今天确实没票」。
    if pool.get("db_error") and not items:
        return {**schema, "available": False, "reason": "pool_unavailable"}

    statuses = _latest_signal_statuses()
    markets: list[dict[str, Any]] = []
    for item in items:
        code = item.get("code")
        closes = a_share_routes.recent_closes(code) if isinstance(code, str) and code else None
        prev_close = closes[0] if closes else None
        last_close = closes[1] if closes else None
        change_pct: float | None = None
        if prev_close:  # 0 或 None 都算取不到：除以 0 会炸，且 0 价本身无意义
            if last_close is not None:
                change_pct = (last_close - prev_close) / prev_close * 100
        markets.append(
            {
                "symbol": code,
                "last_price": last_close,
                "change_pct": change_pct,
                "signal_status": statuses.get(code, "none") if isinstance(code, str) else "none",
                "available": bool(item.get("drawable")),
            }
        )
    return {
        **schema,
        "available": True,
        "as_of": pool.get("as_of"),
        "rows": list(watchlist_rows(markets)),
    }


def make_handler(
    provider: SnapshotProvider | SnapshotSource,
    *,
    llm_limiter: SlidingWindowLimiter | None = None,
    quota_limiter: SlidingWindowLimiter | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create a read-only handler bound to a thread-safe snapshot provider.

    The provider must use independent repository/connection state per request when
    backed by SQLite; this adapter does not serialize concurrent calls for it.

    If ``provider`` additionally exposes ``inspect(bar_index)``, the
    ``/api/dashboard/inspect`` route is enabled for B3 per-bar inspection.

    R59（审计 H1/H3）：``llm_limiter`` / ``quota_limiter`` 是**可注入**的限流器
    （默认各自新建一个，见 :data:`_DEFAULT_LLM_MAX_EVENTS` /
    :data:`_DEFAULT_QUOTA_MAX_EVENTS`）。测试注入假时钟的实例即可验证
    「超限回 429 + Retry-After」而**不必 sleep**；生产调用方（``cpt/web/a_share.py``、
    ``cpt/web/__main__.py``）不传，用默认值。
    """

    # 在类定义**之前**绑定成局部变量：handler 方法引用它们是闭包变量（不是全局），
    # 这样每个 server 实例都持有自己的限流器，测试之间不会互相污染。
    _llm_limiter = llm_limiter or SlidingWindowLimiter(
        max_events=_DEFAULT_LLM_MAX_EVENTS, window_seconds=_DEFAULT_LLM_WINDOW_S
    )
    _quota_limiter = quota_limiter or SlidingWindowLimiter(
        max_events=_DEFAULT_QUOTA_MAX_EVENTS, window_seconds=_DEFAULT_QUOTA_WINDOW_S
    )

    class DashboardHandler(BaseHTTPRequestHandler):
        #: R59（审计 H4）：access log 用的请求开始时刻（``handle_one_request`` 写入）。
        _request_started: float | None = None
        #: R59（审计 M3）：是否已经发出过响应头（兜底 except 不能写第二个响应）。
        _response_started: bool = False

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            # A 股路由**不依赖**加密 provider，必须在 ``provider()`` 之前分发：
            # 否则 (a) 每次 A 股请求都白建一次加密快照，(b) 加密侧不可达时这里
            # 会先 send_error(500)，A 股路由永远走不到。
            if path.path.startswith("/api/dashboard/a-share/"):
                self._handle_a_share_get(path.path, query)
                return
            # Trade API（对 LKL-Trade 交易机的决策投喂）必须排在 provider() **之前**，
            # 理由同 A 股路由：否则 (a) 每次拉决策都白建一次加密快照，
            # (b) 加密侧 provider 不可达时这里直接 500，交易机拿到的是「服务挂了」
            # 而不是「今天没有信号」——两者在上游是**完全不同的处置**。
            if path.path.startswith("/api/trade/"):
                self._handle_trade_get(path.path, query)
                return
            # 我的追踪（Track）也必须在 provider() 之前：与 A 股 / Trade 同理，
            # 否则加密侧不可达时 500 把追踪路由挡在门外。
            if path.path.startswith("/api/dashboard/track"):
                self._handle_track_get(path.path, query)
                return
            try:
                if callable(provider) and not hasattr(provider, "snapshot_payload"):
                    snapshot = provider()
                else:
                    snapshot = provider.snapshot_payload()
            except Exception:  # noqa: BLE001
                self._write_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "snapshot_unavailable", "snapshot unavailable"
                )
                return
            if path.path == "/api/dashboard/health":
                # 有真实健康能力的 provider（realtime）优先：避免静态 "ok": true
                # 掩盖上游不可达或降级。没有该能力的 provider 退回静态只读声明。
                health_fn = getattr(provider, "health", None)
                if callable(health_fn):
                    try:
                        payload: dict[str, Any] = dict(health_fn())
                    except Exception as exc:  # noqa: BLE001 — 健康检查本身失败也要能返回
                        _LOG.warning("provider.health() failed: %s", exc)
                        payload = {
                            "ok": False,
                            "read_only": True,
                            "degraded": True,
                            "last_error": f"health_probe_failed:{type(exc).__name__}",
                        }
                else:
                    payload = {"ok": True, "read_only": True, "degraded": False}
            elif path.path == "/api/dashboard/snapshot":
                payload = dict(snapshot)
                market = dict(payload.get("market", {}))
                runtime = dict(payload.get("runtime", {}))
                # 当 provider 支持 hot-reload（realtime 模式）时，把 ?symbol= / ?interval_ms=
                # 透传给底层 provider，让下一次响应已经是新交易对的数据，
                # 而不是只改 payload 字段、K 线仍为旧交易对。
                # demo / fixture 模式 provider 没有 select_symbol，按下面 fallback 走。
                provider_switched = False
                provider_error: str | None = None
                # R59（审计 H2）：``?symbol=`` / ``?interval_ms=`` 先在入口校验**形状**，
                # 非法立即 400，绝不落到上游。理由：走到 ``select_symbol`` 就已经在
                # HTTP 工作线程里同步跑整条流水线（拉数 + 全量重算 + 写结构事件），
                # 一个畸形查询串就能触发一次完整重算并 commit。
                requested_symbol = ""
                if query.get("symbol"):
                    requested_symbol = query["symbol"][0].strip()
                    if not _SYMBOL_RE.match(requested_symbol):
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_symbol",
                            "symbol 形状非法（形如 BTCUSDT：2..20 位大写字母/数字）",
                        )
                        return
                requested_interval_ms: int | None = None
                if query.get("interval_ms"):
                    try:
                        requested_interval_ms = int(query["interval_ms"][0])
                    except ValueError:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "interval_ms_not_int",
                            "interval_ms must be int",
                        )
                        return
                    if requested_interval_ms <= 0:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_interval_ms",
                            "interval_ms 必须为正数",
                        )
                        return
                if requested_symbol and isinstance(provider, SelectableSource):
                    requested_interval = runtime.get("interval") or "1h"
                    if requested_interval_ms is not None:
                        try:
                            requested_interval = resolve_interval_label(requested_interval_ms)
                        except ValueError:
                            # 原来这里会 ValueError 冒到 except Exception，被折成
                            # provider_warnings 里的 "symbol_switch_failed:..." ——
                            # 参数错却回 200 + 警告，调用方无从判断请求到底生效没有。
                            self._write_json_error(
                                HTTPStatus.BAD_REQUEST,
                                "invalid_interval_ms",
                                "interval_ms 不是已知周期标签",
                            )
                            return
                    try:
                        provider.select_symbol(requested_symbol, requested_interval)
                        provider.force_refresh()
                        provider_switched = True
                    except ValueError:
                        # 形状合法但上游不认识（例如该交易对不存在）→ 这是调用错误，回 400。
                        _LOG.warning(
                            "provider.select_symbol 拒绝 symbol=%s interval=%s",
                            requested_symbol,
                            requested_interval,
                        )
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "unknown_symbol",
                            "上游不认识该 symbol/interval",
                        )
                        return
                    except Exception as exc:  # noqa: BLE001
                        # R59（审计 M4）：只回稳定的代号，异常原文进服务端日志（原来把
                        # 原文塞进 provider_warnings 回给客户端，会带出内部路径/SQL）。
                        provider_error = "symbol_switch_failed"
                        _LOG.warning("provider.select_symbol failed: %s", exc, exc_info=True)
                    else:
                        # 强制刷新后重读 snapshot（已经是新交易对）
                        snapshot = provider.snapshot_payload()
                        payload = dict(snapshot)
                        market = dict(payload.get("market", {}))
                        runtime = dict(payload.get("runtime", {}))
                if not provider_switched:
                    if requested_symbol:
                        market["symbol"] = requested_symbol
                        runtime["symbol"] = requested_symbol
                    if requested_interval_ms is not None:
                        market["interval_ms"] = requested_interval_ms
                payload["market"] = market
                payload["runtime"] = runtime
                if provider_error:
                    payload["provider_warnings"] = [provider_error]
                range_applied = False
                if query.get("start_ms") or query.get("end_ms"):
                    raw_start = (query.get("start_ms") or [""])[0]
                    raw_end = (query.get("end_ms") or [""])[0]
                    try:
                        start_ms = int(raw_start)
                        end_ms = int(raw_end)
                    except ValueError:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "window_not_int",
                            "start_ms/end_ms must be integers",
                        )
                        return
                    if start_ms >= end_ms:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_window",
                            "start_ms must be earlier than end_ms",
                        )
                        return
                    if not isinstance(provider, RangeSource):
                        payload = {"available": False, "reason": "range_unavailable_in_mode"}
                        return self._write_json(payload)
                    try:
                        payload = provider.snapshot_for_range(start_ms, end_ms)
                    except Exception:  # noqa: BLE001
                        self._write_json_error(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            "range_rebuild_failed",
                            "range rebuild failed",
                        )
                        return
                    payload = dict(payload)
                    payload["market"] = dict(payload.get("market", {}))
                    payload["runtime"] = dict(payload.get("runtime", {}))
                    range_applied = True
                if (
                    query.get("level")
                    and not range_applied
                    and isinstance(provider, MultiLevelSource)
                ):
                    try:
                        level = int(query["level"][0])
                    except ValueError:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST, "level_not_int", "level must be an integer"
                        )
                        return
                    try:
                        payload = provider.snapshot_for_level(level)
                    except Exception:  # noqa: BLE001
                        self._write_json_error(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            "level_rebuild_failed",
                            "level rebuild failed",
                        )
                        return
                    payload = dict(payload)
                    payload["market"] = dict(payload.get("market", {}))
                    payload["runtime"] = dict(payload.get("runtime", {}))
                    payload["market"]["symbol"] = query.get(
                        "symbol", [payload["market"].get("symbol", "")]
                    )[0] or payload["market"].get("symbol")
                    payload["runtime"]["symbol"] = payload["runtime"]["symbol"]
                    if query.get("interval_ms"):
                        payload["market"]["interval_ms"] = int(query["interval_ms"][0])
            elif path.path == "/api/dashboard/reproducibility":
                payload = snapshot.get("reproducibility", {})
            elif path.path == "/api/dashboard/parity":
                payload = snapshot.get("parity", {})
            elif path.path == "/api/dashboard/runs":
                # R20：snapshot 本体的 ``runs`` 恒为 []（领域层必须保持「同输入同
                # 输出」），运行历史走 HTTP 响应层。理由见 _with_run_index。
                # R23：优先读 PG 表，所以面板能看到**重启前**的历史；表不可用时
                # _run_index_rows 自己回落到 ring（退化成 R20 行为，不 500）。
                payload = {"runs": _run_index_rows()}
            elif path.path == "/api/dashboard/market-24h":
                payload = snapshot.get("market_24h", {"available": False, "reason": "unavailable"})
            elif path.path == "/api/dashboard/engine-state":
                payload = snapshot.get("engine_state", {})
            elif path.path == "/api/dashboard/inspect":
                bar_index_raw = (query.get("bar_index") or [""])[0]
                try:
                    bar_index = int(bar_index_raw)
                except ValueError:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "bar_index_not_int", "bar_index must be an integer"
                    )
                    return
                if not isinstance(provider, InspectProvider):
                    payload = {"available": False, "reason": "inspect_unavailable_in_mode"}
                    return self._write_json(payload)
                try:
                    payload = provider.inspect(bar_index)
                except IndexError:
                    # R59（审计 M4）：原来把 IndexError 的原文回给客户端（含内部索引
                    # 语义），改成稳定文案；细节只进服务端日志。
                    _LOG.warning("inspect 越界 bar_index=%s", bar_index)
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "inspect_failed", "bar_index 超出当前快照范围"
                    )
                    return
                except Exception:  # noqa: BLE001
                    self._write_json_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR, "inspect_failed", "inspect failed"
                    )
                    return
            elif path.path == "/api/dashboard/sources":
                # R17 数据源能力注册表 + 探活。默认**不**碰消耗额度的源（Wind）：
                # 一次探测就是一次真实额度，必须显式 ?include_quota=1 才允许。
                # 探活结果有 60s 进程内缓存，?refresh=1 强制重探。
                from cpt.adapters.source_registry import (  # noqa: PLC0415
                    SOURCES,
                    capabilities_payload,
                    clear_cache,
                )

                include_quota = (query.get("include_quota") or ["0"])[0] not in {"0", "false", ""}
                # R59（审计 H3）：付费探测要**过闸**。默认路径（不带 include_quota）
                # 一行都不多花，这条既有纪律保持不变；显式要探活时按「来源 + 端点」
                # 限流（默认 3 次 / 5 分钟），超限 429 —— 一次探测就是一次真实 Wind
                # 额度，脚本刷一次就是真金白银。
                if include_quota and self._rate_limit_or_reject(_quota_limiter, "sources_quota"):
                    return
                if (query.get("refresh") or ["0"])[0] not in {"0", "false", ""}:
                    clear_cache()
                markets_raw = (query.get("markets") or [""])[0]
                markets = tuple(part for part in markets_raw.split(",") if part) or None
                # R59（审计 H2）：markets 原来是「原样透传」，未知标识会被静默忽略
                # （``probe_all`` 里 intersect 为空 ⇒ 一个源都不探）——看起来像
                # 「探过了、都没问题」。这里显式拒绝。
                if markets is not None:
                    known = {market for source in SOURCES for market in source.markets}
                    unknown = sorted({market for market in markets if market not in known})
                    if unknown:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "unknown_market",
                            "markets 含未知市场标识：" + ",".join(unknown),
                        )
                        return
                if include_quota:
                    # 审计留痕：付费探测必须能从日志里追溯（谁/什么时候/探了哪几个市场）。
                    _LOG.info(
                        "付费探活 include_quota=1 client=%s markets=%s refresh=%s",
                        client_ip(self),
                        ",".join(markets) if markets else "*",
                        (query.get("refresh") or ["0"])[0],
                    )
                payload = capabilities_payload(include_quota=include_quota, markets=markets)
            elif path.path == "/api/dashboard/signal-radar":
                # Phase N1：信号雷达 —— 只读聚合当前 snapshot 的信号数据。
                # 不新增计算口径，仅把 snapshot 中已有的 signal / signal_first_sell
                # 翻译成前端友好的扁平结构，含状态中文标签与新鲜度。
                from time import time as _now  # noqa: PLC0415

                def _radar_entry(
                    signal: dict[str, Any] | None,
                    code: str,
                    name: str,
                    market: str,
                    source_label: str = "",
                ) -> dict[str, Any] | None:
                    if not signal:
                        return None
                    status = signal.get("status", "none")
                    signal_type = signal.get("signal_type", "first_buy")
                    # 取最晚的时间戳作为 signal_time
                    times = [
                        signal.get("alert_time"),
                        signal.get("candidate_time"),
                        signal.get("confirmed_time"),
                        signal.get("invalidated_time"),
                    ]
                    valid_times = [t for t in times if t is not None]
                    signal_time = max(valid_times) if valid_times else None
                    now_ms = int(_now() * 1000)
                    freshness_ms = now_ms - signal_time if signal_time else None
                    return {
                        "code": code,
                        "name": name,
                        "market": market,
                        "signal_type": signal_type,
                        "status": status,
                        "level": signal.get("level"),
                        "signal_time": signal_time,
                        "freshness_ms": freshness_ms,
                        "price": signal.get("price"),
                        "divergence_status": signal.get("divergence_status"),
                        "source_label": source_label,
                    }

                market_label = (snapshot.get("market", {}) or {}).get("symbol", "")
                market_name = (snapshot.get("market", {}) or {}).get("name", market_label)
                market_type = (
                    "a_share" if market_label and not market_label.endswith("USDT") else "crypto"
                )

                entries = []
                sig = snapshot.get("summary", {}).get("signal") if snapshot.get("summary") else None
                if sig:
                    entry = _radar_entry(sig, market_label, market_name, market_type)
                    if entry:
                        entries.append(entry)
                sig_sell = (
                    snapshot.get("summary", {}).get("signal_first_sell")
                    if snapshot.get("summary")
                    else None
                )
                if sig_sell:
                    entry = _radar_entry(sig_sell, market_label, market_name, market_type, "一卖")
                    if entry:
                        entries.append(entry)

                payload = {
                    "available": True,
                    "signals": entries,
                    "disclaimer": (
                        "本页面为只读结构分析与学习工具，所有「信号/状态」均为缠论结构术语，"
                        "不构成投资建议、要约或任何买卖/持仓建议。市场有风险，投资须谨慎。"
                    ),
                }
            elif path.path == "/api/dashboard/export":
                # Phase 6 P2：时间范围切片导出。范围参数是**必填**的 —— 导出默认全量
                # 会给出几百根 K 线的响应，而调用方要的从来是某个可研究的区间。
                raw_start = (query.get("start_ms") or [""])[0]
                raw_end = (query.get("end_ms") or [""])[0]
                try:
                    start_ms = int(raw_start)
                    end_ms = int(raw_end)
                except ValueError:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "invalid_range", "start_ms/end_ms 必须是整数"
                    )
                    return
                if start_ms >= end_ms:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "invalid_range", "start_ms 必须早于 end_ms"
                    )
                    return
                if not snapshot:
                    payload = {
                        "schema_version": "dashboard_export.v1",
                        "available": False,
                        "reason": "snapshot_unavailable",
                    }
                else:
                    sliced = slice_snapshot(dict(snapshot), start_ms, end_ms)
                    # R32：把切片自己的说明**提到外层信封**。之前这里只写
                    # start_ms/end_ms 两个数，而 slice_snapshot 产出的
                    # candle_count / source_bar_count / unsliced_blocks_note
                    # 全被压在内层 ``snapshot.slice`` 里 —— 消费方第一眼看的是
                    # envelope，于是「只切了 candles、结构块仍是完整窗口」这件
                    # 事完全不可见。start_ms/end_ms 两个键保持不变（纯新增）。
                    slice_meta = dict(sliced.get("slice", {}))
                    slice_meta["start_ms"] = start_ms
                    slice_meta["end_ms"] = end_ms
                    payload = {
                        "schema_version": "dashboard_export.v1",
                        "available": True,
                        "slice": slice_meta,
                        "candle_count": len(sliced.get("candles", [])),
                        "snapshot": sliced,
                    }
            elif path.path == "/api/dashboard/levels":
                # Phase 5 P1：级别递归树。吃的是**带 level 键的结构序列**，
                # 活路径上就是 overlays 的四类结构（asdict 后含 level）。
                raw_overlays = snapshot.get("overlays")
                overlays: dict[str, Any] = raw_overlays if isinstance(raw_overlays, dict) else {}
                structures: list[dict[str, Any]] = []
                for key in ("fractals", "bis", "zhongshus", "trend_types"):
                    chunk = overlays.get(key) if isinstance(overlays, dict) else None
                    if isinstance(chunk, list):
                        structures.extend(chunk)
                if not structures:
                    payload = {
                        "schema_version": "dashboard_levels.v1",
                        "available": False,
                        "reason": "overlays_unavailable",
                    }
                else:
                    payload = {
                        "schema_version": "dashboard_levels.v1",
                        "available": True,
                        "levels": list(level_tree(structures)),
                    }
            elif path.path == "/api/dashboard/compare":
                left_id = (query.get("left") or [""])[0].strip()
                right_id = (query.get("right") or [""])[0].strip()
                if not left_id or not right_id:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST,
                        "missing_run_id",
                        "必须同时提供 left 与 right 两个 run_id",
                    )
                    return
                # R23：**表优先 → ring 兜底**。跨重启时 ring 必然是空的，
                # 表才是活路。注意 ``stored`` 里 key 存在但值为 None 表示
                # 「库里记了没有本体」（4MB 闸门），那种情况**不回落**。
                stored = _load_run_bodies([left_id, right_id])
                left_body = stored[left_id] if left_id in stored else run_body(left_id)
                right_body = stored[right_id] if right_id in stored else run_body(right_id)
                if left_body is None or right_body is None:
                    payload = {
                        "schema_version": "dashboard_compare.v1",
                        "available": False,
                        "reason": "run_body_unavailable",
                    }
                else:
                    compared = compare_snapshots(left_body, right_body)
                    # 摘要化：``differences`` 里绝不允许出现整段 candles/overlays
                    # 原始数组（几百根 K 线的字符串会让前端渲染卡死）。
                    compared["differences"] = _summarize_diff_entries(compared.get("differences"))
                    # 索引行里的 dataset_hash/run_id 比本体里的更权威：本体可能被摘要/
                    # 裁剪过，而 fixture 模式的 runtime 根本不带 run_id（索引行由
                    # `runtime.run_id or dataset_hash` 推导出来），不回填就会是 null。
                    for side, run_id, body in (
                        ("left", left_id, left_body),
                        ("right", right_id, right_body),
                    ):
                        # 索引行里的 dataset_hash/run_id 比本体里的更权威：本体可能
                        # 被摘要/裁剪过，而 fixture 模式的 runtime 根本不带 run_id
                        # （索引行由 `runtime.run_id or dataset_hash` 推导出来），
                        # 不回填就会是 null。R23 追加：跨重启时 ring 空，从本体反推。
                        index_row = find_run(run_id) or _index_row_from_body(run_id, body)
                        compared[f"{side}_run_id"] = index_row.get("run_id") or run_id
                        if index_row.get("dataset_hash") is not None:
                            compared[f"{side}_dataset_hash"] = index_row["dataset_hash"]
                    payload = {
                        "schema_version": "dashboard_compare.v1",
                        "available": True,
                        **compared,
                    }
            elif path.path == "/api/dashboard/multi-run":
                raw_ids = (query.get("run_ids") or [""])[0]
                run_ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
                if not 2 <= len(run_ids) <= 5:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_run_ids",
                        "run_ids 需为 2..5 个逗号分隔的 run_id",
                    )
                    return
                # R23：同 /compare，**表优先 → ring 兜底**。逐个 id 独立判断，
                # 哪边能查到算哪边；全查不到才降级。
                stored = _load_run_bodies(run_ids)
                bodies: list[dict[str, Any]] = []
                for run_id in run_ids:
                    found = stored[run_id] if run_id in stored else run_body(run_id)
                    if found is not None:
                        bodies.append(found)
                if not bodies:
                    payload = {
                        "schema_version": "dashboard_multi_run.v1",
                        "available": False,
                        "reason": "run_body_unavailable",
                    }
                else:
                    payload = {
                        "schema_version": "dashboard_multi_run.v1",
                        "available": True,
                        **align_runs(bodies),
                    }
            elif path.path == "/api/dashboard/llm/calls":
                # LLM 调用审计列表（UI 轮询用）。LLM 未启用时返回空列表而不是 500。
                try:
                    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
                    from cpt.application.llm_cases import list_calls  # noqa: PLC0415

                    subject = (query.get("subject_id") or [""])[0].strip() or None
                    try:
                        limit = int((query.get("limit") or ["20"])[0])
                    except ValueError:
                        limit = 20
                    client = AShareLocalClient()
                    try:
                        payload = list_calls(  # noqa: SLF001
                            client._get_conn(),  # noqa: SLF001
                            limit=limit,
                            subject_id=subject,
                        )
                    finally:
                        client.close()
                except Exception as exc:  # noqa: BLE001 — LLM 是旁路，不可用就降级
                    _LOG.warning("llm calls unavailable: %s", exc)
                    payload = {
                        "schema_version": "dashboard_llm_calls.v1",
                        "available": False,
                        "reason": "llm_unavailable",
                        "count": 0,
                        "calls": [],
                    }
            elif path.path == "/api/dashboard/inspection":
                # R38：运行巡检 + 水位（R31-R38 讨论的"日志分两轨"的落地入口）。
                # 只读旁路：DB 抖动不 500，降级成 available=false + reason。
                try:
                    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
                    from cpt.storage import run_metric_store as _rms  # noqa: PLC0415

                    limit_raw = (query.get("limit") or ["60"])[0]
                    try:
                        limit = int(limit_raw)
                    except ValueError:
                        # ⚠️ 参数非法是**调用错误**，不是 DB 抖动。原来 ``int()``
                        # 裸调，``?limit=abc`` 会掉进下面的 except、回
                        # ``inspection_unavailable`` —— 那个 reason 读起来像
                        # 「库挂了」，排查的人会去查 DB，而真正的原因在 URL 里。
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST, "limit_not_int", "limit 必须是整数"
                        )
                        return
                    # R59（审计 M5）：上限夹到 1..500。原来只有 ``int()``，
                    # ``?limit=100000000`` 会直接把「最近 N 条」变成全表扫描 +
                    # 每个 (market, symbol) 一次 waterline_trend（N+1 放大到天文数字）。
                    limit = max(1, min(limit, _MAX_LIMIT))
                    _client = AShareLocalClient()
                    try:
                        _conn = _client._get_conn()  # noqa: SLF001
                        latest = _rms.latest_inspection(_conn)
                        runs = _rms.recent_metrics(_conn, kind=_rms.KIND_RUN, limit=limit)
                        # R59（审计 M5）：N+1 的规模由 distinct (market,symbol) 决定，
                        # 而它随 run 行数增长。先按首次出现顺序收集 key（超过
                        # _MAX_TREND_KEYS 的部分只计数、不查询），再用**一条**
                        # 批量查询取回全部趋势 —— 20 个 key 从 20 次往返变成 1 次。
                        keys: list[tuple[str, str]] = []
                        seen: set[tuple[str, str]] = set()
                        truncated_keys = 0
                        for _row in runs:
                            _key = (str(_row["market"]), str(_row["symbol"]))
                            if _key in seen:
                                continue
                            seen.add(_key)
                            if len(keys) >= _MAX_TREND_KEYS:
                                # 截断计数按**distinct key**算（旧实现在超限后
                                # 才 add 进 seen，同一 key 每行都会再计一次）。
                                truncated_keys += 1
                                continue
                            keys.append(_key)
                        bulk = _rms.waterline_trends_bulk(_conn, keys, limit=limit)
                        trends: dict[str, Any] = {
                            f"{market}/{symbol}": bulk[(market, symbol)] for market, symbol in keys
                        }
                        payload = {
                            "schema_version": "dashboard_inspection.v1",
                            "available": True,
                            "latest_inspection": dict(latest) if latest else None,
                            "waterlines": list(runs),
                            "trends": trends,
                            "health_values": list(_rms.HEALTH_VALUES),
                            # 审计 M5：让调用方看得出「参数被夹过 / 结果被截断」，
                            # 而不是以为这就是全部。
                            "limits": {
                                "limit": limit,
                                "max_limit": _MAX_LIMIT,
                                "max_trend_keys": _MAX_TREND_KEYS,
                                "trend_keys_truncated": truncated_keys,
                            },
                        }
                    finally:
                        _client.close()
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("inspection unavailable: %s", exc, exc_info=True)
                    payload = {
                        "schema_version": "dashboard_inspection.v1",
                        "available": False,
                        "reason": "inspection_unavailable",
                        # R59（审计 M4）：``detail`` 原来回 ``type(exc).__name__: exc``，
                        # psycopg 的原文里带主机/库名/角色 —— 对外只留稳定代号，
                        # 原文进上面那条 warning 日志（排查信息不丢）。
                        "detail": "internal_error",
                    }
            elif path.path == "/api/dashboard/structure-events":
                # R27：结构事件流列表页（「最近发生了什么」）。与
                # /signal-stats 同一个降级纪律：只读旁路，DB 抖动不 500。
                try:
                    limit = int((query.get("limit") or ["50"])[0])
                except ValueError:
                    limit = 50
                # R59（审计 M5）：非法值保持「回落默认」的既有契约，越界则夹紧。
                limit = max(1, min(limit, _MAX_LIMIT))
                event_type = (query.get("event_type") or [""])[0].strip() or None
                kind = (query.get("kind") or [""])[0].strip() or None
                payload = _structure_events_payload(limit=limit, event_type=event_type, kind=kind)
            elif path.path == "/api/dashboard/structure-events/timeline":
                # 单结构时间线。缺 structure_id 是**调用错误**不是降级，回 400 ——
                # 没有 id 只能返回全表，那不是这个路由的语义。
                structure_id = (query.get("structure_id") or [""])[0].strip()
                if not structure_id:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_structure_id",
                        "structure_id 必填",
                    )
                    return
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    limit = 100
                limit = max(1, min(limit, _MAX_LIMIT))
                payload = _structure_timeline_payload(structure_id, limit=limit)
            elif path.path == "/api/dashboard/signal-stats":
                days_raw = (query.get("days") or ["30"])[0]
                try:
                    days = int(days_raw)
                except ValueError:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "invalid_days", "days 必须是整数"
                    )
                    return
                # R59（审计 M5）：``days`` 原来无上界（``?days=99999999`` 会扫全表）。
                days = max(1, min(days, _MAX_DAYS))
                code_filter = (query.get("code") or [""])[0].strip() or None
                payload = _signal_stats_payload(days, code_filter)
            elif path.path == "/api/dashboard/watchlist":
                # 只读旁路，纪律与同组的 /inspection 一致：DB 抖动不 500，
                # 降级成 available=false + reason。原先这里**裸调**
                # ``_watchlist_payload()``，任何冒出来的异常都会变成 500，
                # 而这条路由的语义本来就是「缺省值优先」。
                try:
                    payload = _watchlist_payload()
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("watchlist unavailable: %s", exc)
                    payload = {
                        "schema_version": "dashboard_watchlist.v1",
                        "available": False,
                        "reason": "watchlist_unavailable",
                        "rows": [],
                    }
            # R51：画布 D（wbt 报告视图）与 `/api/canvas/wbt` 一并下线 ——
            # 按用户指示取消该画布，端点直接除名，外部调用拿到 404 not_found。
            # 留档：原实现见 git f7b0c5fe8:cpt/application/canvas_wbt.py。
            else:
                self._write_json_error(HTTPStatus.NOT_FOUND, "not_found", "")
                return
            self._write_json(_with_run_index(payload))

        # ---------------------------------------------------------- A 股（R17-3）

        def _handle_a_share_get(self, path: str, query: dict[str, list[str]]) -> None:
            """A 股只读路由：snapshot / pool / watchlist / recommendation。"""
            from cpt.web import (
                a_share_routes,  # noqa: PLC0415 — 避免顶层拖入 psycopg
                track_api,  # noqa: PLC0415 — 复用同一个用户标识解析
            )

            # R59（审计 L5）：自选按用户分文件。这里只做「按传入 user 分文件」，
            # **不引入新鉴权**（S2：X-CPT-User 自报零校验，不在本批范围）；
            # 缺头时 track_api 给 DEFAULT_USER_ID，路径仍是老的单文件，行为零变化。
            user_id = track_api.extract_user_id(self.headers)

            if path == "/api/dashboard/a-share/recommendation":
                # R45：结构判断摘要（买卖 + 参考价）。**纯确定性** ——
                # 买卖与价格由 `application.recommendation` 算，**不经 LLM**；
                # LLM 只在另一条 `llm/summarize` 路由上配人话，失败不影响这里。
                code = (query.get("code") or [""])[0].strip()
                if not code:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "code_required", "缺少 code 参数"
                    )
                    return
                level = (query.get("level") or [""])[0].strip()
                hdays = (query.get("history_days") or [""])[0]
                self._write_json(
                    a_share_routes.build_recommendation(
                        code,
                        level=level or None,
                        history_days=int(hdays) if hdays.isdigit() else 0,
                    )
                )
                return

            if path == "/api/dashboard/a-share/snapshot":
                code = (query.get("code") or [""])[0].strip()
                if not code:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "code_required", "缺少 code 参数"
                    )
                    return
                width_raw = (query.get("width_k") or [""])[0]
                width_k = a_share_routes.DEFAULT_WIDTH_K
                if width_raw:
                    try:
                        width_k = int(width_raw)
                    except ValueError:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST, "width_k_not_int", "width_k 必须是整数"
                        )
                        return
                    if not 5 <= width_k <= 2000:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "width_k_out_of_range",
                            f"width_k 超出范围 5..2000：{width_k}",
                        )
                        return
                # R59（审计 M1）：GET 默认**只读**（不联网补因子、不写库）。
                # ``?ensure_factors=1`` 才允许这条请求触发"拉因子 + 落库"——
                # 把写副作用从"默认发生"改成"必须显式要求"。
                ensure_raw = ((query.get("ensure_factors") or [""])[0] or "").strip().lower()
                if ensure_raw in {"1", "true", "yes", "on"}:
                    ensure_factors: bool | None = True
                elif ensure_raw:
                    ensure_factors = False
                else:
                    ensure_factors = None
                try:
                    payload = a_share_routes.snapshot_payload(
                        code, width_k=width_k, ensure_factors=ensure_factors
                    )
                except a_share_routes.InvalidCodeError as exc:
                    # 代码格式非法回 400（JSON 体，见 _write_json_error 的注释：
                    # 中文消息不能走 send_error）。
                    self._write_json_error(HTTPStatus.BAD_REQUEST, "invalid_code", str(exc))
                    return
                self._write_json(_with_run_index(payload))
                return
            if path == "/api/dashboard/a-share/pool":
                self._write_json(a_share_routes.pool_payload(user=user_id))
                return
            if path == "/api/dashboard/a-share/watchlist":
                self._write_json(a_share_routes.watchlist_payload(user=user_id))
                return
            self._write_json_error(HTTPStatus.NOT_FOUND, "not_found", "")

        def _handle_trade_get(self, path: str, query: dict[str, list[str]]) -> None:
            """Trade API 只读路由：decisions / results / health。

            延迟导入 ``cpt.web.trade_api``，理由同 ``_handle_a_share_get``——
            它要 psycopg 与本地库客户端，不能让加密侧看板顶层拖着。
            """
            from cpt.web import trade_api  # noqa: PLC0415 — 避免顶层拖入 psycopg

            qs = urlencode({k: v[0] for k, v in query.items() if v})
            handlers: dict[str, Callable[[str], tuple[dict[str, Any], int]]] = {
                "/api/trade/decisions": trade_api.handle_trade_decisions,
                "/api/trade/results": trade_api.handle_trade_results_get,
                "/api/trade/health": lambda _qs: trade_api.handle_trade_health(),
            }
            handler = handlers.get(path)
            if handler is None:
                self._write_json_error(HTTPStatus.NOT_FOUND, "not_found", "")
                return
            try:
                payload, status = handler(qs)
            except Exception:  # noqa: BLE001 — 上游异常必须变成状态码，不能让 handler 抛出去断连接
                _LOG.exception("trade route failed: %s", path)
                self._write_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "trade_unavailable", "trade api error"
                )
                return
            self._write_json_status(HTTPStatus(status), payload)

        def _handle_trade_post(self) -> bool:
            """``POST /api/trade/results``（交易机回执）；返回 False 表示不是 Trade 路由。"""
            path = urlsplit(self.path).path
            if path != "/api/trade/results":
                return False
            from cpt.web import trade_api  # noqa: PLC0415

            # R59（审计 M2）：回执 POST 原来**完全不看 Content-Type**，任何跨站表单
            # （``enctype=text/plain``）都能打进来。这里按同模块 A 股写路由的做法加门禁，
            # 但**放行「没有 Content-Type」的请求**：
            #
            #   浏览器发起的表单/fetch **必然带** Content-Type（form-urlencoded /
            #   text/plain / multipart），所以门禁照样挡住跨站提交；而交易机
            #   （LKL-Trade，仓外程序）若用 ``data=json.dumps(...)`` 就可能一个头都不带 ——
            #   硬拒会直接把真金白银的决策回执打回去，代价远大于收益。
            #   缺头时留一条 warning，等确认上游一定会带头再收紧。
            content_type = self.headers.get("Content-Type") or ""
            if content_type and "application/json" not in content_type.lower():
                self._write_json_error(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "content_type_required",
                    "Content-Type 必须是 application/json",
                )
                return True
            if not content_type:
                _LOG.warning("trade results POST 未带 Content-Type（已放行，交易机兼容）")
            try:
                payload, status = trade_api.handle_trade_results_post(self._read_json_body())
            except Exception:  # noqa: BLE001
                _LOG.exception("trade results post failed")
                self._write_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "trade_unavailable", "trade api error"
                )
                return True
            self._write_json_status(HTTPStatus(status), payload)
            return True

        def _handle_track_get(self, path: str, query: dict[str, list[str]]) -> None:
            """我的追踪：list / advice / history。

            延迟导入 :mod:`cpt.web.track_api`，理由同 :meth:`_handle_a_share_get` —
            psycopg 不能进加密侧看板的顶层。
            """
            from cpt.web import track_api  # noqa: PLC0415

            user_id = track_api.extract_user_id(self.headers)
            routed = track_api.route_for(path)
            if routed is None:
                self._write_json_error(HTTPStatus.NOT_FOUND, "not_found", "")
                return
            handler_name, code = routed
            try:
                if handler_name == "list":
                    payload, status = track_api.handle_track_list(user_id)
                elif handler_name == "advice":
                    payload, status = track_api.handle_track_advice(user_id, code)
                elif handler_name == "history":
                    days_raw = (query.get("days") or [""])[0] or str(
                        track_api.track_store.SNAPSHOT_RETENTION_DAYS
                    )
                    try:
                        days = int(days_raw)
                    except ValueError:
                        days = track_api.track_store.SNAPSHOT_RETENTION_DAYS
                    payload, status = track_api.handle_track_history(user_id, code, days=days)
                elif handler_name == "maintenance":
                    # R59（审计 M18）：``deploy/cron/track-maintenance-daily.sh`` 每天
                    # 07:30 UTC 真调 ``?apply=1``；接线前它每天 404。契约键名不能改。
                    # 只有 ``1``/``true``（大小写无关）才是真删，**其余取值一律 dry-run**：
                    # 清理是破坏性动作，宁可少删一次（第二天再来），
                    # 也不能因为参数写法不认识就把用户的回收站行删了。
                    apply_raw = ((query.get("apply") or [""])[0] or "").strip().lower()
                    payload, status = track_api.handle_track_maintenance(
                        apply=apply_raw in {"1", "true"}
                    )
                else:
                    self._write_json_error(HTTPStatus.NOT_FOUND, "not_found", "")
                    return
            except Exception:  # noqa: BLE001
                _LOG.exception("track get failed path=%s user=%s", path, user_id)
                self._write_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "track_unavailable", "track api error"
                )
                return
            self._write_json_status(HTTPStatus(status), payload)

        def _handle_track_speak(self) -> bool:
            """``POST /api/dashboard/track/{code}/speak`` —— LLM 重写人话。"""
            from cpt.web import track_api as _track_api  # noqa: PLC0415

            path = urlsplit(self.path).path
            parts = [seg for seg in path.split("/") if seg]
            if (
                len(parts) != 5
                or parts[0] != "api"
                or parts[1] != "dashboard"
                or parts[2] != "track"
                or parts[4] != "speak"
            ):
                return False
            user_id = _track_api.extract_user_id(self.headers)
            try:
                payload, status = _track_api.handle_track_speak(user_id, parts[3])
            except Exception:  # noqa: BLE001
                _LOG.exception("track speak failed path=%s user=%s", path, user_id)
                self._write_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "track_unavailable",
                    "track speak api error",
                )
                return True
            if status >= 400:
                self._write_json_error(
                    HTTPStatus(status),
                    payload.get("error", "speak_failed"),
                    payload.get("detail") or payload.get("code", ""),
                )
            else:
                self._write_json_status(HTTPStatus(status), payload)
            return True

        def _handle_track_post(self) -> bool:
            """我的追踪写操作：add / remove / restore。返回 False 表示不是 Track 写路由。"""
            path = urlsplit(self.path).path
            # 显式写三个路径字面量：让 ``check_doc_drift`` 的字面 grep 能找到，
            # 也比 ``any(f"...{op}" ...)`` 一眼看出"这三条归这里管"。
            if path == "/api/dashboard/track/add":
                op = "add"
            elif path == "/api/dashboard/track/remove":
                op = "remove"
            elif path == "/api/dashboard/track/restore":
                op = "restore"
            else:
                return False

            from cpt.web import track_api  # noqa: PLC0415

            user_id = track_api.extract_user_id(self.headers)
            # R59（2026-10-09 日志巡检 A）：``_read_json_body`` 契约是「读不到就返回
            # None、不抛」，原来用 try/except 兜 Exception 等于没兜住——body 为 None
            # 会一路传进 handle_track_add 触发 AttributeError，被下面的宽 except 变成
            # 500。畸形/空/非对象 body 是客户端问题，这里必须回 400。
            body = self._read_json_body()
            if not isinstance(body, dict):
                self._write_json_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_json",
                    "request body must be a JSON object",
                )
                return True
            try:
                if op == "add":
                    payload, status = track_api.handle_track_add(user_id, body)
                elif op == "remove":
                    payload, status = track_api.handle_track_remove(user_id, body)
                else:  # op == "restore"
                    payload, status = track_api.handle_track_restore(user_id, body)
            except Exception:  # noqa: BLE001
                _LOG.exception("track post failed path=%s user=%s", path, user_id)
                self._write_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "track_unavailable", "track api error"
                )
                return True
            self._write_json_status(HTTPStatus(status), payload)
            return True

        def _handle_a_share_write(self, method: str) -> bool:
            """A 股自选与 LLM 解释的写操作；返回 False 表示这不是 A 股路由。"""
            path = urlsplit(self.path)
            if path.path not in (
                "/api/dashboard/a-share/watchlist",
                "/api/dashboard/a-share/llm/explain",
                "/api/dashboard/a-share/llm/summarize",
            ):
                return False
            # 审计 M1：写接口无鉴权（单用户看板经 nginx 暴露）。至少要求
            # ``Content-Type: application/json``——HTML 表单 / 简单请求只能发
            # form-urlencoded / text/plain，而声明 json 的跨站 fetch 会触发
            # CORS 预检并失败。不引入鉴权系统，仅抬高跨站触发门槛。
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type.lower():
                # 415（不是 400）：缺/错的 Content-Type 是"媒体类型不受支持"，
                # 与 code 非法（400 invalid_code）区分开，客户端能精确归因。
                self._write_json_error(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "content_type_required",
                    "写接口要求 Content-Type: application/json",
                )
                return True
            from cpt.web import a_share_routes  # noqa: PLC0415
            from cpt.web import track_api as _track_api  # noqa: PLC0415

            # R59（审计 L5）：自选写操作同样按用户分文件（理由见 _handle_a_share_get）。
            user_id = _track_api.extract_user_id(self.headers)
            query = parse_qs(path.query)
            code = (query.get("code") or [""])[0].strip()
            if not code:
                self._write_json_error(HTTPStatus.BAD_REQUEST, "code_required", "缺少 code 参数")
                return True

            if path.path == "/api/dashboard/a-share/llm/summarize":
                # R59（审计 H1）：LLM 端点先过限流。key 是「来源 IP + 端点」，
                # 超限直接 429 + Retry-After，**绝不**让请求走到付费调用上。
                if self._rate_limit_or_reject(_llm_limiter, "llm_summarize"):
                    return True
                # R45：给结构判断配人话。**入队即返回**，不等模型。
                # 与 explain 不同的是它**只吃推荐那几行**，不含结构明细 ——
                # 模型因此没有机会产出与确定性结果**矛盾**的判断
                # （两个数字并排显示时，没人知道该信哪个）。
                if method != "POST":
                    self._write_json_error(
                        HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "仅支持 POST"
                    )
                    return True
                rec = self._read_json_body() or {}
                if not isinstance(rec, dict) or not rec:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST,
                        "recommendation_required",
                        "recommendation 不能为空",
                    )
                    return True
                try:
                    payload = a_share_routes.submit_llm_summarize(code, rec)
                except a_share_routes.InvalidCodeError as exc:
                    # R59（审计 M3）：修前这条分支**完全不校验 code**，`?code=ZZZZZZ`
                    # 会真的去调一次付费 LLM。校验失败回 400 invalid_code，
                    # 而不是让异常直穿到 do_POST（那会变成连接重置）。
                    self._write_json_error(HTTPStatus.BAD_REQUEST, "invalid_code", str(exc))
                    return True
                self._write_json(payload)
                return True

            if path.path == "/api/dashboard/a-share/llm/explain":
                # R59（审计 H1）：同 summarize，LLM 端点先过限流。
                if self._rate_limit_or_reject(_llm_limiter, "llm_explain"):
                    return True
                # LLM 解释：入队即返回，不等模型（实测 provider 延迟 0.3–7.4s）。
                if method != "POST":
                    self._write_json_error(
                        HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "仅支持 POST"
                    )
                    return True
                structure = self._read_json_body() or {}
                if not isinstance(structure, dict) or not structure:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "structure_required", "structure 不能为空"
                    )
                    return True
                try:
                    payload = a_share_routes.submit_llm_explain(code, structure)
                except a_share_routes.InvalidCodeError as exc:
                    # R59（审计 M3）：``submit_llm_explain`` 里的 ``_normalize`` 在
                    # ``try`` 之外**故意**抛，这里必须接住 —— 修前它直穿 do_POST，
                    # 客户端看到的是连接被重置而不是 400。
                    self._write_json_error(HTTPStatus.BAD_REQUEST, "invalid_code", str(exc))
                    return True
                self._write_json(payload)
                return True

            try:
                if method == "POST":
                    payload = a_share_routes.watchlist_add(code, user=user_id)
                else:
                    payload = a_share_routes.watchlist_remove(code, user=user_id)
            except a_share_routes.InvalidCodeError as exc:
                self._write_json_error(HTTPStatus.BAD_REQUEST, "invalid_code", str(exc))
                return True
            self._write_json(payload)
            return True

        #: 读 body 的上限。LLM 解释请求带的是**一个结构对象**（几 KB），
        #: 32MB 足够宽松，同时挡住「把整个 snapshot 塞进来」这种误用 ——
        #: candles 有几十万根，塞进来既慢又烧 token。
        _MAX_BODY_BYTES: int = 32 * 1024 * 1024

        def _read_json_body(self) -> Any:
            """读 JSON 请求体；**读不到或解析失败返回 ``None``**（不抛）。

            调用方据此回自己的 400。刻意不抛：body 畸形是客户端问题，
            不该在 handler 里冒一个未捕获异常变成 500。
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return None
            if length <= 0 or length > self._MAX_BODY_BYTES:
                return None
            try:
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                _LOG.warning("读取 JSON body 失败: %s", exc)
                return None

        def _rate_limit_or_reject(self, limiter: SlidingWindowLimiter, label: str) -> bool:
            """超限时写 429 并返回 ``True``（调用方 ``return``）；放行返回 ``False``。

            R59（审计 H1/H3）：bucket key 是「端点标签 + 对端 IP」，**不含**
            ``X-CPT-User``（S2 身份自报、零校验，拿它当键等于送一个绕过开关，
            见 :mod:`cpt.web.rate_limit` 的说明）。429 带 ``Retry-After``
            与 ``X-RateLimit-*``，客户端/脚本能算出该等多久。
            """
            source = client_ip(self)
            decision = limiter.check(f"{label}:{source}")
            if decision.allowed:
                return False
            _LOG.warning(
                "限流命中 endpoint=%s client=%s 上限=%d/%gs",
                label,
                source,
                limiter.max_events,
                limiter.window_seconds,
            )
            self._write_json_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "rate_limited",
                f"请求过于频繁（{label} 最多 {limiter.max_events} 次 / "
                f"{int(limiter.window_seconds)}s），请稍后重试",
                headers={
                    "Retry-After": str(decision.retry_after),
                    "X-RateLimit-Limit": str(limiter.max_events),
                    "X-RateLimit-Remaining": "0",
                },
            )
            return True

        def _write_json(self, payload: dict[str, Any]) -> None:
            self._write_json_status(HTTPStatus.OK, payload)

        def _write_json_error(
            self,
            status: HTTPStatus,
            code: str,
            message: str,
            *,
            headers: dict[str, str] | None = None,
        ) -> None:
            """以 JSON 体返回错误。

            **不要用 ``send_error`` 传非 ASCII 文本**：``BaseHTTPRequestHandler``
            把 message 写进 HTTP 状态行，而状态行只能 latin-1 编码 —— 中文消息会
            抛 ``UnicodeEncodeError`` 并**直接断开连接**，浏览器侧只看到网络错误
            （实测踩到：``code=abc`` 的 400 变成了 RemoteDisconnected）。

            :param headers: 额外响应头（R59 新增，429 的 ``Retry-After`` 用）。
            """
            self._write_json_status(
                status, {"error": {"code": code, "message": message}}, headers=headers
            )

        def _write_json_status(
            self,
            status: HTTPStatus,
            payload: dict[str, Any],
            *,
            headers: dict[str, str] | None = None,
        ) -> None:
            # R59（审计 L5）：204 / 304 **不得带 body**（RFC 7230 §3.3.2 还禁止带
            # Content-Length）。原来 DELETE 软删不存在的票时回 204 却照写 JSON 体，
            # 严格客户端（含 requests 的 keep-alive 复用）会把这段体当成下一个响应
            # 的开头，连接随之错位。
            #
            # 被拒方案：把 track_api 的 204 改成 200。那会破坏「DELETE 幂等用 204
            # 表达」的既有契约，而且 204 带体这个 bug 在别的路由上也存在，改出口
            # 一次是修根因。
            if status in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED):
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                return
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError):
                self._write_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "payload_not_json_safe",
                    "payload is not JSON-safe",
                )
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802
            try:
                # Trade 回执先于 A 股写路由判定：两者路径前缀不重叠，顺序无关，
                # 但放在前面是为了让「Trade API 只 POST 一个端点」这件事在代码上一眼可见。
                if self._handle_trade_post():
                    return
                # Track 「再讲一次人话」是 POST + 路径里带 code，先于 _handle_track_post
                # 判定（后者只认 /add /remove /restore 三条字面量）。
                if self._handle_track_speak():
                    return
                if self._handle_track_post():
                    return
                if self._handle_a_share_write("POST"):
                    return
                self._write_json_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "")
            except Exception:
                self._handle_unexpected("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            try:
                if self._handle_a_share_write("DELETE"):
                    return
                self._write_json_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "")
            except Exception:
                self._handle_unexpected("DELETE")

        def _handle_unexpected(self, method: str) -> None:
            """写路由的**最后一道**兜底（R59／审计 M3）。

            原来 ``do_POST`` 没有任何 except：路由里漏了一个校验（例如
            ``a_share_routes.submit_llm_explain`` 在 try 之前调的 ``_normalize``
            抛 ``InvalidCodeError``）就会一路冒到 socketserver —— 客户端看到的是
            **连接被重置**，而不是一个能读的 400/500。这里兜住并回结构化 500。

            已经发过响应头就不再写第二遍（半截响应会污染 keep-alive 流）。
            """
            _LOG.exception("%s 未捕获异常 path=%s", method, self.path)
            if self._response_started:
                return
            self._write_json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "服务器内部错误"
            )

        def handle_one_request(self) -> None:
            """记录请求开始时刻，供 access log 算耗时（R59／审计 H4）。"""
            self._request_started = time.monotonic()
            super().handle_one_request()

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            """access log（R59／审计 H4）：方法 / 脱敏路径 / 状态 / 耗时 / 来源。

            原来全站**没有任何访问日志** —— ``log_message`` 直接 ``return``，
            线上排障只能靠猜。这里不覆盖 ``send_response`` 而是覆写官方的
            ``log_request`` 钩子：它在 ``send_response`` 里被调用，状态码天然准确。

            路径经 :func:`_redact_path_for_log` 脱敏（``?token=`` / ``?webhook=``
            等的值不会落盘）；耗时用 ``time.monotonic``，不受系统时间调整影响。
            """
            self._response_started = True
            started = self._request_started
            elapsed_ms = "-" if started is None else f"{(time.monotonic() - started) * 1000:.1f}"
            _LOG.info(
                "access %s %s %s status=%s size=%s elapsed_ms=%s",
                client_ip(self),
                self.command or "-",
                _redact_path_for_log(self.path),
                code,
                size,
                elapsed_ms,
            )

        def log_message(self, format: str, *args: object) -> None:
            """把 ``BaseHTTPRequestHandler`` 的 stderr 日志改走 ``logging``（R59／审计 H4）。

            只用于 ``log_error`` 路径（404/501 这类由 ``send_error`` 发起的响应），
            参数里不含查询串；正常请求的访问日志在 :meth:`log_request`。
            """
            _LOG.info("http %s - %s", client_ip(self), format % args)

    return DashboardHandler


def serve_snapshot(
    provider: SnapshotProvider | SnapshotSource,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    max_workers: int = _DEFAULT_MAX_WORKERS,
    llm_limiter: SlidingWindowLimiter | None = None,
    quota_limiter: SlidingWindowLimiter | None = None,
) -> ThreadingHTTPServer:
    """Build a server; caller owns lifecycle and must call ``server_close``.

    R59（审计 H4）：返回的 server 是 :class:`_BoundedThreadingHTTPServer`
    （并发上限 ``max_workers``，默认 :data:`_DEFAULT_MAX_WORKERS`=32，
    超限的连接当场 503 + ``Retry-After``）。返回标注仍是
    :class:`ThreadingHTTPServer` —— 子类满足该类型，调用方（
    ``cpt/web/__main__.py`` 与 ``cpt/web/a_share.py``）无需改。
    修前这里直接实例化 ``ThreadingHTTPServer``：并发上限在线上**完全没生效**
    （一个慢 LLM 请求叠几十个并发就能把线程开爆）。

    ``max_workers <= 0`` 表示不设限（测试用；生产默认必须有界）。

    R59（审计 H1/H3）：``llm_limiter`` / ``quota_limiter`` 透传给
    :func:`make_handler`，让测试可以注入**假时钟**的限流器验证 429 路径
    （不必 sleep，也不再需要为了拿到限流器去直接实例化私有 server 类）。
    """
    return _BoundedThreadingHTTPServer(
        (host, port),
        make_handler(provider, llm_limiter=llm_limiter, quota_limiter=quota_limiter),
        max_workers=max_workers,
    )
