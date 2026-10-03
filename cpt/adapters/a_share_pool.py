"""A 股热门池 + 自选（R15-4）。

按 plan §5.4：

- **热门池合成** = ``public.hot_rank`` 最新日全集（top100）∪ ``public.ladder_day``
  最新日 ``cont_days >= 2``。``public.limit_pool_em`` 仅 30 天窗口，**不足以做主
  池**，这里只是辅助查询入口（保留 ``list_limit_pool_marks`` 给上层做"今日涨停
  辅助标注"用）。合成结果默认**全量**，由调用方用 ``limit`` 收敛（A 股下拉只要
  Top5）。
- **自选** = 落盘在服务端 JSON（默认 ``~/.cache/cpt/watchlist.json``，可用环境变量
  ``CPT_WATCHLIST`` 覆盖）。**不是 localStorage** —— 2026-09-25 修掉的正是这个：
  手输的代码此前只写进 URL 查询串，第二次登录就没了。

## 取舍
- 本模块**只读 DB**，不做任何写库。本模块只暴露**纯查询接口 + 自选 JSON
  读写**——后者是文件 IO，不走 DB 避免 schema 膨胀。
- 自选 JSON 用 ``pathlib.Path`` + ``fcntl`` 文件锁，避免并发写损坏。
- 自选**没有用户概念**：单用户看板，全库一份。多用户要换成带 user 键的实现。
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import tempfile
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


def fetch_hot_pool(conn: Any, *, limit: int | None = None) -> list[HotPoolEntry]:
    """热门池 = ``hot_rank`` 最新日 ∪ ``ladder_day`` 最新日 cont_days≥2。

    :param limit: 只取排序后的前 N 条（``None`` = 全部）。A 股下拉默认只要 Top5
        （2026-09-25 需求：热门池收敛到 Top5），但 ``None`` 保留全量能力 ——
        ``limit_pool_em`` 那类辅助查询和将来的"展开全部"都要用到。
    """
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
    )[:limit]


def fetch_limit_pool_marks(conn: Any, trade_date: str | None = None) -> list[LimitPoolMark]:
    """``public.limit_pool_em`` 当日涨停池（**辅助标签**）。

    该表只有 30 天窗口（plan §5.2 表行），不足以做主池。本接口给上层做"今日
    涨停辅助标注"——例如在自选列表上标"今日涨停"小角标。
    """
    if trade_date is None:
        with conn.cursor() as cur:
            cur.execute("SELECT max(date) FROM public.limit_pool_em")
            row = cur.fetchone()
        if not row or row[0] is None:
            # 表为空（首次回填前的正常窗口）：返回干净空态，别让 None.isoformat() 崩。
            return []
        trade_date = row[0].isoformat()

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
    """自选 JSON 落盘存储（``fcntl.flock`` 同机**跨进程**劝告锁）。

    flock 锁挂在打开文件描述上，同机各进程独立 ``open`` 的 FD 之间正常互斥，
    因此**不止进程内安全**；但它不跨机、在 NFS 上不可靠——多机/网络盘需要换成
    走 DB 的实现。本接口设计为**可注入**，方便后续替换。
    """

    def __init__(self, path: pathlib.Path | str) -> None:
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("[]", encoding="utf-8")

    @property
    def _lock_path(self) -> pathlib.Path:
        """旁路锁文件（``<data>.lock``）：随数据文件同目录，永不参与原子替换。"""
        return self._path.with_name(f"{self._path.name}.lock")

    def _lock(self) -> Any:
        # 锁挂在**旁路锁文件**上而非数据文件：数据文件用 ``os.replace`` 原子替换会
        # 换 inode，若锁在被替换的 inode 上，等待中的进程会拿到已 unlink 的旧 inode
        # 读到陈旧数据（且 Windows 上无法替换一个正被打开的路径）。独立锁文件
        # inode 稳定，flock 的跨进程互斥始终成立。
        self._lock_path.touch(exist_ok=True)
        f = self._lock_path.open("a", encoding="utf-8")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    @staticmethod
    def _unlock(f: Any) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()

    def _write_atomic(self, data: list[dict[str, Any]]) -> None:
        """同目录临时文件写全 + ``os.replace`` 原子替换，避免写途中被杀导致半截 JSON。"""
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _read(self) -> list[dict[str, Any]]:
        """读并解析自选文件。**损坏一律抛，绝不猜。**

        R45 修。原来的三个方法对「文件损坏」有**三种不同反应**：

            list()    → 抛 WatchlistError        ✅ 诚实
            add()     → ``data = []`` 继续写       ❌ **静默清空整个自选股**
            remove()  → ``return False``          ❌ 谎称「没删掉」

        ``add`` 那条最严重：用户在文件损坏后加一只票，**原有的全部条目被静默
        覆盖掉**，且没有日志、没有异常 —— 用户以为只是加了一票，实际丢了全部。
        实测（2026-10-03）：损坏文件上 ``add("000002")`` 之后，文件里只剩
        ``000002`` 一条。

        「猜成空列表」在这里是**最坏**的降级：自选股是用户数据，猜错就是丢数据。
        正确做法是响亮报错，让人去修文件（文件损坏通常来自手工编辑或外部工具）。
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise WatchlistError(
                f"自选文件已损坏（{self._path}）：{e}。"
                f"**未做任何修改** —— 请先修好或移走该文件再试。"
            ) from e
        except OSError as e:
            raise WatchlistError(f"自选文件读取失败: {e}") from e
        if not isinstance(data, list):
            raise WatchlistError(
                f"自选文件格式不对（{self._path}）：顶层应是数组，实得 {type(data).__name__}"
            )
        return data

    def list(self) -> list[WatchlistEntry]:
        return [WatchlistEntry(**item) for item in self._read()]

    def add(self, code: str, market: str) -> WatchlistEntry:
        entry = WatchlistEntry(
            code=code,
            market=market,
            added_at=datetime.now(UTC).isoformat(),
        )
        f = self._lock()
        try:
            data = self._read()
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
            self._write_atomic(data)
        finally:
            self._unlock(f)
        return entry

    def remove(self, code: str, market: str) -> bool:
        f = self._lock()
        try:
            data = self._read()
            new_data = [
                e for e in data if not (e.get("code") == code and e.get("market") == market)
            ]
            removed = len(new_data) != len(data)
            if removed:
                self._write_atomic(new_data)
            return removed
        finally:
            self._unlock(f)
