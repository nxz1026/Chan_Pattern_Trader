"""Dashboard 运行持久化：``public.cpt_dashboard_run`` 跨重启可比的冷路径。

**为什么需要本模块（R23）**：``/api/dashboard/compare?left=&right=`` 与
``/api/dashboard/multi-run?run_ids=`` 需要按 ``run_id`` 取回快照**本体**，
而 ``cpt/application/dashboard_runs.py`` 的 ``_RUN_BODIES`` 是 ``deque`` +
``maxlen=50`` 的**进程级**缓冲——重启即空，用户重启后只能看到
``{"available": false, "reason": "run_body_unavailable"}``。本模块把同一次
``record_run`` 命中**同步双写**进 PG，重启后仍可查。

**与 ring 的关系**（两套并存，不是替代）：

======================  =========================================  ==================
                        ``_RUN_RING`` / ``_RUN_BODIES``（ring）      本表（cold-path）
======================  =========================================  ==================
存活期                  进程内，重启即空                            跨重启
容量                    ``maxlen=50``，约 25 分钟运行历史            append-only，无自动 GC
去重                    30s 内同 ``(run_id, dataset_hash)`` 命中     ``ON CONFLICT (run_id)``
                        同一份缓存 snapshot **快路径不写库**         ``DO NOTHING`` 天然幂等
写入失败                不影响 HTTP（本来就是内存操作）              best-effort，不抛给调用点
======================  =========================================  ==================

写路径 ``upsert_run`` 由调用点（``cpt/web/app.py``）包在 try/except 里吞掉；
**读路径必须抛** ``DashboardRunError``——「库读不到」与「确实没有这条 run」是两
件事，前者不能冒充后者，调用方据此决定是否回落到 ring。

**连接由调用方传入**（与 R21 ``signal_event_store`` 同风格）：本模块不碰
``AShareLocalClient``、不 import psycopg——CI 只跑 ``pip install -e .``（不带
``[db]``），顶层拖入 psycopg 会让整个包的测试在 collection 阶段就炸。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_LOG = logging.getLogger(__name__)

__all__ = [
    "DashboardRunError",
    "get_snapshots",
    "recent_runs",
    "upsert_run",
]


class DashboardRunError(RuntimeError):
    """运行持久化表读写失败。"""


def _ms_to_timestamptz(ms: Any) -> datetime | None:
    """``generated_at`` → UTC ``datetime``；**解析不出来返回 ``None``**。

    ``dashboard_runs.record_run`` 的口径是 Unix **毫秒**（int/float），但
    ``build_run_index`` 直接透传 ``reproducibility.generated_at``，而
    fixtures / demo 模式下它可能是 ISO 字符串或 ``None``。

    ⚠️ **这里原来回落墙钟**（只记一条 warning）。后果不是「时间戳略偏」，而是
    索引里堆起一批**看起来完全正常的假时间戳**：它们参与 ``ORDER BY
    generated_at DESC``、参与运维的 ``DELETE WHERE generated_at < ...`` 窗口，
    没人能再分辨哪条是假的。所以解析不出来就**不写**（见 :func:`upsert_run`），
    宁可少一行，也不要一个假的「这一轮跑在什么时候」。

    Args:
        ms: Unix 毫秒（int/float）/ ISO 串 / ``datetime`` / ``None``。
    Returns:
        UTC ``datetime``；任何解析不出来的形态（含 ``None`` / 空串 / 布尔 / 非正
        毫秒）都给 ``None``。
    """
    if isinstance(ms, datetime):
        return ms if ms.tzinfo else ms.replace(tzinfo=UTC)
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    if isinstance(ms, str) and ms.strip():
        try:
            parsed = datetime.fromisoformat(ms.strip().replace("Z", "+00:00"))
        except ValueError:
            _LOG.warning("generated_at 不是可解析的 ISO 时间串 %r", ms)
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if ms is not None:
        _LOG.warning("generated_at 类型异常 %r", type(ms).__name__)
    return None


def _timestamptz_to_ms(value: Any) -> int | None:
    """UTC ``datetime`` → Unix 毫秒；``None`` / 非 datetime → ``None``。"""
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return None


def _to_int(value: Any) -> int | None:
    """jsonb ``->>`` 抽出来的文本 → ``int``；抽不出整数就诚实地给 ``None``。

    **在 Python 侧转而不是在 SQL 里 ``::integer``**：``->>`` 返回 text，脏数据
    （``"interval_ms": "abc"``）会让 ``::integer`` 直接把整条查询打成 SQL 错误，
    ``/api/dashboard/runs`` 整页 500。转不动就少一个字段，比整页挂掉好。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


#: ``recent_runs`` 的 SELECT 列顺序，必须与 :func:`_row_to_run_index` 一致。
_RUN_INDEX_COLUMNS = (
    "run_id",
    "dataset_hash",
    "generated_at",
    "body_recorded",
    "symbol",
    "interval_ms",
    "bar_count",
    "config_hash",
    "source",
    "status",
)

#: 不读 jsonb 主体，只抽元数据。``->>`` 取 text，数值转换交给 Python（见 ``_to_int``）。
_RUN_INDEX_SQL = """SELECT run_id, dataset_hash, generated_at, body_recorded,
                             snapshot->'market'->>'symbol'        AS symbol,
                             snapshot->'market'->>'interval_ms'   AS interval_ms,
                             snapshot->'market'->>'bar_count'    AS bar_count,
                             snapshot->'reproducibility'->>'config_hash' AS config_hash,
                             snapshot->'runtime'->>'data_source'  AS source,
                             snapshot->'runtime'->>'status'      AS status
                      FROM public.cpt_dashboard_run
                      ORDER BY generated_at DESC
                      LIMIT %s"""


def _row_to_run_index(row: tuple[Any, ...]) -> dict[str, Any]:
    """把一行投影成与 :func:`cpt.application.dashboard_runs.build_run_index` **同形状**的 dict。

    形状必须对齐，因为 ``/api/dashboard/runs`` 的前端（``dashboard.js``）对
    ring 与表两条数据源一视同仁。特别注意两个口径：

    1. ``generated_at`` 统一出 Unix **毫秒**（库里是 timestamptz）。
    2. ``created_at`` **不落列**（5 列方案刻意不加），但前端读的是它，所以这里
       补一份与 ``generated_at`` 同值的镜像——与 ``record_run`` 里的
       「同值双写」处置保持一致，否则前端在表数据源上会读到 ``undefined``。
    """
    out: dict[str, Any] = {
        "run_id": row[0],
        "dataset_hash": row[1],
        "generated_at": _timestamptz_to_ms(row[2]),
        "body_recorded": bool(row[3]),
        "symbol": row[4],
        "interval_ms": _to_int(row[5]),
        "bar_count": _to_int(row[6]),
        "config_hash": row[7],
        "source": row[8],
        "status": row[9],
    }
    out["created_at"] = out["generated_at"]
    return out


def upsert_run(conn: Any, row: dict[str, Any], body: dict[str, Any] | None) -> bool:
    """把一次 ``record_run`` 命中写进表，返回 ``True`` 表示新增了一行。

    :param conn: psycopg 连接（**不 commit**，事务边界由调用方控制）。
    :param row: ``record_run`` 返回的索引行；只需要 ``run_id`` /
        ``dataset_hash`` / ``generated_at`` 三个键。
    :param body: 快照本体；``None`` 表示被 ``RUN_BODY_MAX_BYTES`` 闸门拒了，
        此时写 ``body_recorded=false`` + ``snapshot=NULL``。
    :returns: ``True`` = 新增行；``False`` = ``run_id`` 已存在（``DO NOTHING`` 命中）
        **或** 因 ``generated_at`` 解析不出而明确拒绝落库。
    :raises DashboardRunError: 写入失败。**调用方必须 best-effort 吞掉**——
        记一次账失败不该让用户的 HTTP 响应 500。

    ``ON CONFLICT (run_id) DO NOTHING``：同一份快照被 30s 轮询重复命中时，
    ``run_id`` 不变，天然只留一行。**不要**改成 ``DO UPDATE``——那会让
    ``generated_at`` 被后来的请求刷新成「刚刚」，运行历史的时间轴就废了。
    """
    run_id = str(row.get("run_id") or "").strip()
    if not run_id:
        # run_id 是主键，空串会让「全部写入失败」且日志刷屏。
        # record_run 口径是 runtime.run_id or dataset_hash；两者都空有两类来源：
        # ① 占位快照（无数据可哈希，属预期）② 真异常快照——分级记录，不抛。
        # 索引行自带 status/bar_count（build_run_index 从 runtime/market 平铺投影）。
        if row.get("status") == "empty":
            _LOG.debug("占位快照无 run_id，跳过持久化（dataset_hash=%r）", row.get("dataset_hash"))
        else:
            _LOG.warning(
                "运行索引行缺 run_id，跳过持久化（dataset_hash=%r status=%r bar_count=%r）",
                row.get("dataset_hash"),
                row.get("status"),
                row.get("bar_count"),
            )
        return False

    dataset_hash = str(row.get("dataset_hash") or "")
    generated_at = _ms_to_timestamptz(row.get("generated_at"))
    if generated_at is None:
        # ⚠️ 「写 NULL」在这张表上等于「这一行写不进去」：``generated_at`` 是
        # ``NOT NULL``（见 ``scripts/migrations/2026-10-02_r23_dashboard_run.sql``）。
        # 与其让 PG 抛 ``null value in column "generated_at"``（看不出原值是什么），
        # 不如在这里**明确拒绝**并留下一条 ERROR —— 这一次运行**不进**冷路径，
        # 索引里也不会出现一个看起来很真的假时间戳。调用方 best-effort 吞掉即可
        # （HTTP 不受影响，ring 里那份索引行仍在）。
        _LOG.error(
            "generated_at 解析不出（%r），不写入假时间戳，本次运行不进冷路径 run_id=%s",
            row.get("generated_at"),
            run_id,
        )
        return False
    body_recorded = body is not None
    # jsonb 用「json.dumps + %s::jsonb 强转」而不是 psycopg 的 Jsonb 包装器：
    # 后者要 import psycopg，而本模块刻意保持零 psycopg 依赖（见模块 docstring）。
    snapshot = json.dumps(body, default=str) if body_recorded else None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.cpt_dashboard_run
                     (run_id, dataset_hash, generated_at, body_recorded, snapshot)
                   VALUES (%s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (run_id) DO NOTHING""",
                (run_id, dataset_hash, generated_at, body_recorded, snapshot),
            )
            inserted = int(cur.rowcount or 0)
    except Exception as exc:
        _LOG.warning("写入运行持久化失败 run_id=%s: %s", run_id, exc)
        raise DashboardRunError(f"写入运行持久化失败: {exc}") from exc

    return inserted > 0


def get_snapshots(conn: Any, run_ids: list[str]) -> dict[str, dict[str, Any] | None]:
    """按 ``run_id`` 批量取快照本体，返回 ``{run_id: snapshot | None}``。

    **返回值语义（调用方必须读懂）**：

    - **key 不存在** = 库里没有这一行 → 调用方应回落到 in-process ring。
    - **key 存在但 value 是 ``None``** = 有这行，但本体被 4MB 闸门拒了
      （``body_recorded=false``）→ **不要**回落到 ring，直接按拿不到本体处理。
      回落会让「库里明确记了没有本体」被「ring 里碰巧有一份」掩盖，掩盖掉
      闸门本身的存在。

    一次 ``WHERE run_id = ANY(%s)`` 走主键，不做 N 次单查。
    """
    ids = [str(rid).strip() for rid in run_ids if str(rid).strip()]
    if not ids:
        return {}

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, snapshot FROM public.cpt_dashboard_run WHERE run_id = ANY(%s)",
                (list(ids),),
            )
            rows = cur.fetchall() or ()
    except Exception as exc:
        _LOG.warning("读取运行本体失败 run_ids=%s: %s", ids, exc)
        raise DashboardRunError(f"读取运行本体失败: {exc}") from exc

    return {row[0]: row[1] for row in rows}


def recent_runs(conn: Any, limit: int = 50) -> tuple[dict[str, Any], ...]:
    """最近 ``limit`` 次运行，**时间倒序**（最新在前），形状与 ring 的 ``recent_runs`` 对齐。

    只 SELECT 抽出来的元数据，**不读 jsonb 主体**——运行索引面板要的是列表，
    30KB 一份的本体在 50 行规模下就是 1.5 MB 白白过线。

    :raises DashboardRunError: 读失败。调用方据此回落到 ring。
    """
    capped = max(1, min(int(limit), 500))
    try:
        with conn.cursor() as cur:
            cur.execute(_RUN_INDEX_SQL, (capped,))
            rows = cur.fetchall() or ()
    except Exception as exc:
        _LOG.warning("读取运行索引失败 limit=%s: %s", capped, exc)
        raise DashboardRunError(f"读取运行索引失败: {exc}") from exc

    return tuple(_row_to_run_index(row) for row in rows)
