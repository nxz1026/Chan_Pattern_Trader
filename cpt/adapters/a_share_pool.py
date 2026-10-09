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
  ``fcntl`` 只在 POSIX 存在，模块顶部有平台守卫（见 ``WatchlistStore``）。
- 自选**没有用户概念**：单用户看板，全库一份。多用户要换成带 user 键的实现。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

try:  # pragma: no cover - 平台分支
    # ⚠️ ``fcntl`` 是 **POSIX 独有**。原来它在模块顶层无条件 import，于是整个
    # ``a_share_pool`` 在 Windows 上直接 ImportError —— 而它同时被
    # ``/api/dashboard/a-share/pool`` 与自选路由依赖，连「别的路由都起不来」。
    # 生产是 Linux，锁的语义一个字都不改；这里只是让模块在别的平台**可导入**
    # （锁退化为无操作，见 :meth:`WatchlistStore._lock`）。
    import fcntl  # noqa: PLC0415
except ImportError:  # pragma: no cover - Windows / 无 fcntl 的解释器
    fcntl = None  # type: ignore[assignment]

_LOG = logging.getLogger(__name__)

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
        row = cur.fetchone()
        if row is None or row[0] is None:
            # R59（审计 M7）：空表时 ``max(date)`` 返回 NULL。旧代码把它直接塞进
            # ``WHERE date = %s``（psycopg 适配成 ``= NULL``，永远查不到行），
            # 再由 ``hot_date.isoformat()`` 在 ``AttributeError`` 上炸 ——
            # 那是「首次回填前」的正常窗口，不是故障。照
            # :func:`fetch_limit_pool_marks` 的守卫返回干净空态。
            hot_date = None
            hot_rows: list[Any] = []
        else:
            hot_date = row[0]
            cur.execute(
                "SELECT code, rank FROM public.hot_rank WHERE date = %s ORDER BY rank",
                (hot_date,),
            )
            hot_rows = cur.fetchall()

        cur.execute("SELECT max(date) FROM public.ladder_day")
        row = cur.fetchone()
        if row is None or row[0] is None:
            # 同上的守卫：ladder_day 空表不该把热门池整体带崩。
            lad_date = None
            lad_rows: list[Any] = []
        else:
            lad_date = row[0]
            cur.execute(
                """SELECT code, cont_days FROM public.ladder_day
                   WHERE date = %s AND cont_days >= 2 ORDER BY cont_days DESC""",
                (lad_date,),
            )
            lad_rows = cur.fetchall()

    entries: dict[str, HotPoolEntry] = {}
    if hot_date is not None:
        for code, rank in hot_rows:
            entries[code] = HotPoolEntry(
                code=code,
                source="hot_rank",
                rank=int(rank),
                cont_days=None,
                as_of=hot_date.isoformat(),
            )
    if lad_date is not None:
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


#: 一条自选记录的**全部**字段（R59 审计 L8）。多一个字段会让
#: ``WatchlistEntry(**item)`` 抛 ``TypeError``；少一个则要么 ``TypeError``，
#: 要么在 ``e.get(...)`` 上抛 ``AttributeError`` —— 都不是调用方认识的
#: :class:`WatchlistError`。
_ENTRY_FIELDS: Final[tuple[str, ...]] = ("code", "market", "added_at")


def _entry_problem(item: Any) -> str | None:
    """返回这条原始自选记录的形状问题；形状可构造 :class:`WatchlistEntry` 时返回 ``None``。

    R59（审计 L8）：文件是手改/外部工具写过的，反序列化后必须逐条校验形状，
    不能假设每条都是 ``{"code": str, "market": str, "added_at": str}``。
    """
    if not isinstance(item, dict):
        return f"不是对象（{type(item).__name__}）"
    missing = [f for f in _ENTRY_FIELDS if f not in item]
    if missing:
        return f"缺少字段 {missing}"
    extra = sorted(set(item) - set(_ENTRY_FIELDS))
    if extra:
        return f"出现未知字段 {extra}"
    for field in ("code", "market"):
        value = item[field]
        if not isinstance(value, str) or not value:
            return f"字段 {field!r} 不是非空字符串（{value!r}）"
    added_at = item["added_at"]
    if not isinstance(added_at, str):
        return f"字段 'added_at' 不是字符串（{added_at!r}）"
    return None


class WatchlistStore:
    """自选 JSON 落盘存储（``fcntl.flock`` 同机**跨进程**劝告锁）。

    flock 锁挂在打开文件描述上，同机各进程独立 ``open`` 的 FD 之间正常互斥，
    因此**不止进程内安全**；但它不跨机、在 NFS 上不可靠——多机/网络盘需要换成
    走 DB 的实现。本接口设计为**可注入**，方便后续替换。

    ⚠️ ``fcntl`` 只在 POSIX 存在（见模块顶部的平台守卫）。拿不到它时**仍可用**，
    只是没有跨进程互斥：写入仍是「同目录临时文件 + ``os.replace``」的原子替换，
    损坏/半截 JSON 的防护不受影响，受影响的只是并发写的最后写入者胜出。
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
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    @staticmethod
    def _unlock(f: Any) -> None:
        if fcntl is not None:
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

        R59（审计 L8）补：这里只保证**顶层**是数组。逐条记录的字段形状由
        :func:`_entry_problem` 校验 —— ``list()`` 跳过坏记录并 warning（只读展示），
        ``add`` / ``remove`` 走 :meth:`_assert_entries_sane` 抛
        :class:`WatchlistError`（写路径绝不静默丢数据）。
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

    def _assert_entries_sane(self, data: list[Any]) -> None:
        """写路径的逐条形状校验（R59 审计 L8）。

        为什么写路径**不**像 :meth:`list` 那样跳过坏记录：``add`` / ``remove``
        随后会 ``_write_atomic(data)`` 把整个列表落盘，跳过等于把坏记录**永久
        抹掉** —— 那正是 R45 花一整段 docstring 禁止的「静默丢用户数据」。
        所以这里响亮报错，且消息里带上「未做任何修改」与行号，便于用户修文件。
        """
        for index, item in enumerate(data):
            problem = _entry_problem(item)
            if problem is not None:
                raise WatchlistError(
                    f"自选文件第 {index} 条记录不可用（{problem}）：{self._path}。"
                    f"**未做任何修改** —— 跳过它会连带丢数据，请先修好该文件再试。"
                )

    def list(self) -> list[WatchlistEntry]:
        # R59（审计 L8）：只读路径**逐条校验、跳过坏记录**并留日志痕迹。
        # 旧代码 ``WatchlistEntry(**item)`` 遇到一条坏记录就让整个自选列表抛
        # TypeError（非 dict 行更是 AttributeError），看板整块自选直接消失。
        # 只读展示「能显示几条显示几条」比「全都不显示」有用得多；写路径的
        # 严格性由 :meth:`_assert_entries_sane` 保证，数据不会被悄悄改写。
        entries: list[WatchlistEntry] = []
        for index, item in enumerate(self._read()):
            problem = _entry_problem(item)
            if problem is not None:
                _LOG.warning(
                    "自选文件第 %d 条记录不可用（%s），已跳过：%r", index, problem, str(item)[:120]
                )
                continue
            entries.append(
                WatchlistEntry(code=item["code"], market=item["market"], added_at=item["added_at"])
            )
        return entries

    def add(self, code: str, market: str) -> WatchlistEntry:
        entry = WatchlistEntry(
            code=code,
            market=market,
            added_at=datetime.now(UTC).isoformat(),
        )
        f = self._lock()
        try:
            data = self._read()
            self._assert_entries_sane(data)
            # 幂等：已存在则返回原 entry
            for item in data:
                if item["code"] == code and item["market"] == market:
                    return WatchlistEntry(
                        code=item["code"], market=item["market"], added_at=item["added_at"]
                    )
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
            self._assert_entries_sane(data)
            new_data = [e for e in data if not (e["code"] == code and e["market"] == market)]
            removed = len(new_data) != len(data)
            if removed:
                self._write_atomic(new_data)
            return removed
        finally:
            self._unlock(f)
