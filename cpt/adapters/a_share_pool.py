"""A 股热门池 + 自选（R15-4）。

按 plan §5.4：

- **热门池合成** = ``public.hot_rank`` 最新日全集（top100）∪ ``public.ladder_day``
  最新日 ``cont_days >= 2``。``public.limit_pool_em`` 仅 30 天窗口，**不足以做主
  池**，这里只是辅助查询入口（保留 ``list_limit_pool_marks`` 给上层做"今日涨停
  辅助标注"用）。
- **自选** = 即时生效（前端 localStorage）+ 可选落盘（服务端 JSON 文件
  ``data/watchlist.json``）。

## 取舍
- 本模块**只读 DB**，写库另由 ``cpt.storage.repository`` 系列模块负责。本
  模块只暴露**纯查询接口 + 自选 JSON 读写**——后者是文件 IO，不走 DB 避免
  schema 膨胀。
- 自选 JSON 用 ``pathlib.Path`` + ``fcntl`` 文件锁，避免并发写损坏。
"""

from __future__ import annotations

import fcntl
import json
import logging
import pathlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "HotPoolEntry",
    "LimitPoolMark",
    "WatchlistStore",
    "WatchlistEntry",
    "fetch_hot_pool",
    "fetch_limit_pool_marks",
    "WatchlistError",
]

logger = logging.getLogger("a_share_pool")


# --------------------------------------------------------------------------- #
# DB 工具（与 scripts/factor_backfill.py 同步）
# --------------------------------------------------------------------------- #


def _read_dbconfig() -> dict[str, str]:
    p = pathlib.Path.home() / ".dbconfig"
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("$") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def connection_kwargs() -> dict[str, Any]:
    cfg = _read_dbconfig()
    if not cfg.get("$RDSHOST") or not cfg.get("$DB_PW"):
        raise WatchlistError("~/.dbconfig 缺失 $RDSHOST 或 $DB_PW")
    return {
        "host": cfg["$RDSHOST"],
        "port": int(cfg.get("$DBPORT", "5432")),
        "dbname": cfg.get("$DBNAME", "longkonglong"),
        "user": cfg.get("$USER", "postgres"),
        "password": cfg["$DB_PW"],
        "connect_timeout": 15,
    }


# --------------------------------------------------------------------------- #
# 热门池
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HotPoolEntry:
    """一只票在热门池中的一项记录。"""

    code: str
    source: str  # "hot_rank" | "ladder_day"
    rank: int | None  # hot_rank 的名次（ladder_day 时为 None）
    cont_days: int | None  # ladder_day 的连板天数（hot_rank 时为 None）
    as_of: str  # ISO date（最新可用日）


@dataclass(frozen=True)
class LimitPoolMark:
    """东财 ``limit_pool_em`` 当日涨停池的辅助标签。"""

    code: str
    name: str | None
    cont_days_em: int | None
    pool_type: str | None
    trade_date: str


def fetch_hot_pool(conn: Any) -> list[HotPoolEntry]:
    """热门池 = ``hot_rank`` 最新日 ∪ ``ladder_day`` 最新日 cont_days≥2。"""
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM public.hot_rank")
        hot_date = cur.fetchone()[0]
        cur.execute(
            "SELECT code, rank FROM public.hot_rank WHERE date = %s ORDER BY rank",
            (hot_date,),
        )
        hot_rows = cur.fetchall()

        cur.execute("SELECT max(date) FROM public.ladder_day")
        lad_date = cur.fetchone()[0]
        cur.execute(
            """SELECT code, cont_days FROM public.ladder_day
               WHERE date = %s AND cont_days >= 2 ORDER BY cont_days DESC""",
            (lad_date,),
        )
        lad_rows = cur.fetchall()

    entries: dict[str, HotPoolEntry] = {}
    for code, rank in hot_rows:
        entries[code] = HotPoolEntry(
            code=code,
            source="hot_rank",
            rank=int(rank),
            cont_days=None,
            as_of=hot_date.isoformat(),
        )
    for code, cont_days in lad_rows:
        # hot_rank 优先（带 rank 字段），ladder_day 仅补缺
        if code not in entries:
            entries[code] = HotPoolEntry(
                code=code,
                source="ladder_day",
                rank=None,
                cont_days=int(cont_days),
                as_of=lad_date.isoformat(),
            )
    return sorted(
        entries.values(),
        key=lambda e: (
            e.rank if e.rank is not None else 9999,  # hot_rank 在前
            -e.cont_days if e.cont_days is not None else 0,  # 连板天数高的在前
            e.code,
        ),
    )


def fetch_limit_pool_marks(conn: Any, trade_date: str | None = None) -> list[LimitPoolMark]:
    """``public.limit_pool_em`` 当日涨停池（**辅助标签**）。

    该表只有 30 天窗口（plan §5.2 表行），不足以做主池。本接口给上层做"今日
    涨停辅助标注"——例如在自选列表上标"今日涨停"小角标。
    """
    if trade_date is None:
        with conn.cursor() as cur:
            cur.execute("SELECT max(date) FROM public.limit_pool_em")
            trade_date = cur.fetchone()[0].isoformat()

    with conn.cursor() as cur:
        cur.execute(
            """SELECT code, name, cont_days_em, pool_type
               FROM public.limit_pool_em WHERE date = %s ORDER BY code""",
            (trade_date,),
        )
        rows = cur.fetchall()
    return [
        LimitPoolMark(
            code=r[0],
            name=r[1],
            cont_days_em=int(r[2]) if r[2] is not None else None,
            pool_type=r[3],
            trade_date=trade_date,
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# 自选 JSON 存储
# --------------------------------------------------------------------------- #


class WatchlistError(RuntimeError):
    """自选存储错误（IO / JSON / 文件锁等）。"""


@dataclass(frozen=True)
class WatchlistEntry:
    """自选里的一只票。"""

    code: str  # 6 位裸码 / 或 "600519.SH"
    market: str  # "A" | "crypto"
    added_at: str  # ISO datetime


class WatchlistStore:
    """自选 JSON 落盘存储（fcntl 进程内锁，**单进程安全**）。

    多进程 / 多机需要替换为 ``cpt.storage.repository`` 走 DB——本接口设计为
    **可注入**，方便后续替换。
    """

    def __init__(self, path: pathlib.Path | str) -> None:
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("[]", encoding="utf-8")

    def _lock(self) -> Any:
        f = self._path.open("r+", encoding="utf-8")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    @staticmethod
    def _unlock(f: Any) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()

    def list(self) -> list[WatchlistEntry]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise WatchlistError(f"自选文件读取失败: {e}") from e
        return [WatchlistEntry(**item) for item in data]

    def add(self, code: str, market: str) -> WatchlistEntry:
        entry = WatchlistEntry(
            code=code,
            market=market,
            added_at=datetime.now(UTC).isoformat(),
        )
        f = self._lock()
        try:
            try:
                data = json.loads(f.read())
            except json.JSONDecodeError:
                data = []
            # 幂等：已存在则返回原 entry
            for e in data:
                if e.get("code") == code and e.get("market") == market:
                    return WatchlistEntry(**e)
            data.append(
                {
                    "code": entry.code,
                    "market": entry.market,
                    "added_at": entry.added_at,
                }
            )
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=2)
        finally:
            self._unlock(f)
        return entry

    def remove(self, code: str, market: str) -> bool:
        f = self._lock()
        try:
            try:
                data = json.loads(f.read())
            except json.JSONDecodeError:
                return False
            new_data = [
                e for e in data if not (e.get("code") == code and e.get("market") == market)
            ]
            removed = len(new_data) != len(data)
            if removed:
                f.seek(0)
                f.truncate()
                json.dump(new_data, f, ensure_ascii=False, indent=2)
            return removed
        finally:
            self._unlock(f)
