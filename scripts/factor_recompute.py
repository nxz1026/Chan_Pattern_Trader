"""按公司行动**重算**后复权因子（R37 落地；R39 起默认源是东财）。

## 为什么要有这个脚本

现在的 ``asel.ref_adjust_factor`` 存的是 **``腾讯后复权收盘 ÷ 库里不复权收盘``**
这个**逐日比值**。而后复权因子按定义应在一次除权事件内**恒定**（分段常数）。

R36 实测的后果（全部真机量过）：

- 该比值本身不是常数 —— 600519 在 125 个交易日里从 6.749131 漂到 7.170869
  （相对极差 **6.25%**）；000001 在 20 天内 195.54~200.57（**2.57%**）；
- 绕开解析器直接读腾讯原始返回，**腾讯自己的 hfq 与它自己的不复权就不是等比的**；
- 于是本地 open/high/low 带 0.1%~0.9% 的畸变，而分型/笔端点对这些值敏感
  ⇒ parity 的结构匹配率只有 **22%~55%**。

**离线修不了**（R37 干跑）：比值序列是「漂移 + 偶发真台阶」，阈值法把漂移当台阶
（600825 在最近 4 天里被检出 4 个"台阶"，其实是每天约 −0.1% 的漂移）。

**唯一真值是公司行动本身**。于是按定义算（公式与实现见
:func:`cpt.adapters.a_share_factor.factor_from_actions`）：

    f = Π_{除权日 e}  (1 + 送转比例_e) / (1 − 每股派息_e / 除权前收盘_e)

这三样由 ``--source`` 选的数据源提供（东财 / Wind），见
:class:`_EastmoneySource` 与 :class:`_WindSource` 的说明。

## 数据源（R39 起默认东财）

| 源 | 说明 |
|---|---|
| **东财**（默认） | ``RPT_SHAREBONUS_DET``，免费、从大阪直连 200、**无额度** |
| Wind | ``get_stock_events``，要积分 —— 实测 24/2197 只就撞「账户积分余额不足」，
  只留作交叉校验（``--source wind``） |

⚠️ 东财有两个单位陷阱（都在 ``cpt/adapters/eastmoney_actions.py`` 注释里）：
``PRETAX_BONUS_RMB`` 是**每 10 股**；``BONUS_IT_RATIO`` 已是**送+转总数**，
不能再和 ``IT_RATIO`` 相加（R41 曾因此把因子算大一倍）。

## 安全边界（这个脚本刻意做不到的那几件事）

- **绝不写** ``asel.ref_adjust_factor``（生产表）—— 只写暂存表
  ``asel.ref_adjust_factor_v2``；
- **绝不自动切换** —— 切换是人工决定（看报告 → 决定）；
- **单轮有处理上限**（``--max-calls``，默认 6000）。⚠️ R42：默认源是东财、
  **没有额度**，所以它现在只是「别让一轮跑太久」的时间预算（全集 5223 只
  约 87 分钟），不是「分几天跑完」的机制。用 ``--source wind`` 时它才重新
  变成真正的硬边界（Wind 的 ``RATE_LIMIT_ERROR``）；
- **通道不可用会立即停**并落盘进度（东财侧=接口不可达，Wind 侧=额度/通道）；
- **可断点续跑**（``~/.cache/cpt/factor_recompute_state.json``）；
- **候选集与优先级由 ``--scope`` 决定**（R42）。默认 ``placeholder`` =
  **从没算过的票优先**（``source IS NULL``），其次自选、热门池 —— 理由是
  占位票的因子恒为 1.0，后复权价 == 不复权价，每个除权日的价格跳空都被
  当成**真实下跌**喂给缠论。⚠️ 此前这里写的是「自选 → 热门池 → 其余」，
  而候选集是 ``{source IS NOT NULL}`` —— **只重算已经有真值的那批**，
  占位票一只都碰不到，声明要修的问题和实际在做的事对不上；
- 单只票失败只记账，不中断整轮。

## 用法

    # 看看进度
    .venv/bin/python scripts/factor_recompute.py --report

    # 跑一轮（默认 scope=placeholder，即优先补从没算过的票）
    .venv/bin/python scripts/factor_recompute.py

    # 扩到所有有 bar 的票（3098 -> 5223 只）
    .venv/bin/python scripts/factor_recompute.py --scope all

    # 调试单只（不写暂存表）
    .venv/bin/python scripts/factor_recompute.py --only 000001 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpt.adapters.a_share_factor import (  # noqa: E402
    codes_with_factors,
    ensure_recompute_stage,
    factor_from_actions,
    hot_pool_codes,
    load_recent_closes,
    save_recompute_factors,
)
from cpt.adapters.a_share_local import AShareLocalClient  # noqa: E402
from cpt.adapters.eastmoney_actions import (  # noqa: E402
    EastmoneyActionClient,
    EastmoneyActionError,
    EastmoneyActionUnavailable,
)
from cpt.adapters.wind_source import (  # noqa: E402
    WindQuotaError,
    WindSourceClient,
    WindSourceError,
    WindUnavailableError,
)

_LOG = logging.getLogger("factor_recompute")


class _Source:
    """取数源的统一形状：``fetch_corporate_actions`` + **自己那套**异常分类。

    分类跟着源走，而不是写死成 Wind 的那几个类 —— 否则换源时
    ``except`` 分支会静默失配：源换了、异常类型没换，于是新源的错误
    落进「未知异常」分支被当普通失败重试。R39 实测踩到过一次
    （给 ``WindSourceClient`` 临时挂属性，忘了 ``name``，直接 AttributeError）。
    """

    name = ""
    fatal_errors: tuple[type[BaseException], ...] = ()
    transient_errors: tuple[type[BaseException], ...] = ()

    def fetch_corporate_actions(self, code: str) -> tuple[Any, ...]:
        raise NotImplementedError


class _EastmoneySource(_Source):
    """把东财客户端包成与 :class:`WindSourceClient` **同形**的取数源。

    R39 起东财是**默认**源：Wind 要积分（实测 24/2197 只就撞「账户积分余额不足」），
    东财免费且从大阪直连 200。Wind 保留为 ``--source wind`` 的交叉校验。

    东财没有额度概念，所以「fatal」= 接口不可达 / 返回坏数据 ——
    那时重试同样没意义。
    """

    name = "eastmoney"
    # 解析类失败是确定性的（再发一次还是同样的坏数据）-> 不重试
    fatal_errors = (EastmoneyActionError,)
    # 网络不可达/读超时是瞬时的 -> 有界重试。
    # R40 实测：原先把两者混为一谈且一律不重试，一次 15s 读超时就
    # 把 2189 只的整轮批次掐停了，而那一次重试本可以拿到数据。
    transient_errors = (EastmoneyActionUnavailable,)

    def __init__(self) -> None:
        self._client = EastmoneyActionClient()

    def fetch_corporate_actions(self, code: str) -> tuple[Any, ...]:
        # 东财只要 6 位，而 Wind 要 ``600519.SH``；这里统一收口，别让调用方记两套。
        return self._client.fetch_actions(code.split(".")[0][:6])


class _WindSource(_Source):
    """Wind 客户端的同形包装。额度类**绝不重试**（重试只是白烧积分）。"""

    name = "wind"
    fatal_errors = (WindQuotaError, WindUnavailableError)
    transient_errors = (WindSourceError,)

    def __init__(self) -> None:
        self._client = WindSourceClient()

    def fetch_corporate_actions(self, code: str) -> tuple[Any, ...]:
        return self._client.fetch_corporate_actions(code)


#: 进度文件（断点续跑）
STATE_PATH: Final[Path] = Path(
    os.getenv("CPT_FACTOR_RECOMPUTE_STATE", "~/.cache/cpt/factor_recompute_state.json")
).expanduser()

#: 默认单轮处理上限。
#:
#: ⚠️ R42：它**不再是「额度」**，只是时间预算。R39 起默认源换成东财 ——
#: 免费、无额度，于是这个数字唯一的作用是「别让一轮跑太久」。
#: 全部 5222 只按实测 ~1 只/秒约 87 分钟，远在 cron 的 10h ``timeout`` 内，
#: 所以默认值给到 ``6000``（足够一轮跑完全集）。
#: 用 ``--source wind`` 时它才重新变成**真正的硬边界**（Wind 的
#: ``RATE_LIMIT_ERROR``），此时应当把 ``CPT_RECOMPUTE_MAX_CALLS`` 调小。
DEFAULT_MAX_CALLS: Final[int] = 6000

#: 自选文件（``WatchlistStore`` 用的同一个路径）
WATCHLIST_PATH: Final[Path] = Path(
    os.getenv("CPT_WATCHLIST", "~/.cache/cpt/watchlist.json")
).expanduser()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"done": [], "failed": {}, "days": {}}
    try:
        raw: dict[str, Any] = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOG.warning("进度文件损坏，重开一轮：%s", STATE_PATH)
        return {"done": [], "failed": {}, "days": {}}
    return raw


def bind_state_to_source(state: dict[str, Any], source_name: str) -> dict[str, Any]:
    """把进度**按真值源分账**——换源就重开一轮，不继承别的源的 ``done``。

    ## 为什么必须分账

    ``done`` 的含义是「这只票的因子已经用**某一个源**算过并写进暂存表」。
    换源之后这个含义就变了：Wind 算过的 24 只在东财口径下**并不算已算** ——
    继承过来会让这 24 只被直接跳过，于是暂存表里留下两种口径混写的因子，
    而报告看上去还「一切正常」。

    这是**静默**的：进度文件、暂存表、报告三者都不报错，只有逐行对账才看得出来。

    实测代价为零：Wind 那 24 只本来就没跑完（2197 里的 24），重开一轮不亏。
    """
    previous = state.get("source")
    if previous == source_name:
        return state
    if previous:
        _LOG.warning(
            "真值源从 %s 换成 %s，进度**不继承**（done=%d 只将重跑）",
            previous,
            source_name,
            len(state.get("done") or []),
        )
    return {"done": [], "failed": {}, "days": {}, "source": source_name, "scope": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def watchlist_codes() -> list[str]:
    """自选代码（**JSON 文件**，不是表 —— R24 起自选走 ``WatchlistStore``）。"""
    if not WATCHLIST_PATH.exists():
        return []
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items", data) if isinstance(data, dict) else data
    out: list[str] = []
    for it in items or []:
        code = str(it.get("code", "")) if isinstance(it, dict) else str(it)
        code = code.split(".")[0]
        if code and code not in out:
            out.append(code)
    return out


#: ``--scope`` 的三个取值
SCOPES: Final[tuple[str, ...]] = ("placeholder", "all", "wired")


def codes_with_bars(conn: Any) -> tuple[str, ...]:
    """本地有 K 线的裸码（升序）—— 能算因子的**全集**。

    因子是按 ``public.daily_bar`` 逐根算的，没有 bar 就算不出来，所以候选集
    的天然上界就是这张表的 distinct code。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT code FROM public.daily_bar")
        return tuple(sorted({str(r[0]).split(".")[0] for r in cur.fetchall()}))


def placeholder_codes(conn: Any) -> tuple[str, ...]:
    """生产因子**从未算过**的票（``source IS NULL``）。

    这是 R42 挖出来的最大一个洞：这些票的 ``hfq_factor`` 恒为 1.0 ⇒
    后复权价 == 不复权价 ⇒ 每个除权日的价格跳空被当成**真实下跌**喂给缠论。
    2026-10-02 实测 5222 只里 **3025 只**是这种状态，也就是 58% 的 A 股宇宙
    在用未复权价算结构。

    而它**恰恰**是重算唯一能救的：没有别的数据源，也不需要额度。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT code FROM asel.ref_adjust_factor WHERE source IS NULL")
        return tuple(sorted({str(r[0]).split(".")[0] for r in cur.fetchall()}))


def priority_codes(conn: Any, scope: str = "placeholder") -> list[str]:
    """候选代码，按「重算收益从大到小」排序，去重保序。

    ## ⚠️ R42 修：候选集曾经**排除了全部 3025 只占位票**

    原来第三组是 ``codes_with_factors``（= ``WHERE source IS NOT NULL``），
    也就是**已经有真值的那 2197 只**。于是「因子表 58% 是占位值」这个
    被写进文档、也当作本轮重算主要理由的问题，**一只都碰不到** ——
    声明的目标和实际做的事对不上。

    真实机数据（2026-10-02）：

    ==========================  ====  ==========================================
    分桶                          只数   含义
    ==========================  ====  ==========================================
    ``source IS NULL``           3025   占位，因子恒 1.0 —— **重算收益最大**
    ``source='tx:fqkline'``      2125   有真值但**非单调**（>0.1% 口径）
    ``source='tx:fqkline'``        72   有真值且单调
    ==========================  ====  ==========================================

    ## 现在的排序与 scope

    1. **占位票**（``scope='placeholder'`` 默认）—— 它们现在是错的，重算能修好；
    2. 自选 / 热门池 —— 人在看的，总要新鲜；
    3. 其余有 bar 的票（``scope='all'``）。

    ``scope='wired'`` 保留旧行为（只重算已有真值的），仅供对照复现。
    """
    groups: list[Any] = []
    if scope == "placeholder":
        groups.append(placeholder_codes(conn))
    elif scope == "wired":
        groups.append(codes_with_factors(conn))
    else:  # all
        groups.append(placeholder_codes(conn))
    groups.append(watchlist_codes())
    groups.append(hot_pool_codes(conn))
    if scope == "all":
        groups.append(codes_with_bars(conn))

    order: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for c in group:
            c = str(c).split(".")[0]
            if c and c not in seen:
                seen.add(c)
                order.append(c)
    return order


@dataclass
class CodeResult:
    code: str
    ok: bool
    rows: int = 0
    steps: int = 0
    note: str = ""


def current_latest_factor(conn: Any, code: str) -> float | None:
    """库里**最新一根**的因子 —— 重标定的目标锚点。取不到就返回 ``None``（不重标定）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT hfq_factor FROM asel.ref_adjust_factor WHERE code=%s "
            "ORDER BY trade_date DESC LIMIT 1",
            (code,),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] else None


def anchor_scale(steps: dict[str, float], anchor_factor: float | None) -> float:
    """算出「整段序列」的重标定系数 ``k``，使**最新一根**的因子等于 ``anchor_factor``。

    ## 基准是什么

    :func:`~cpt.adapters.a_share_factor.factor_from_actions` 从最新一次除权往前乘，
    所以它产出的 ``fmap[ex_date]`` 作用于**该除权日之前**的 bar，而
    **最后一次除权之后**（含最新一根）的 bar 因子就是定义式里的 ``1.0``。

    ⇒ 整段序列的最新一根原始值 = 1.0，所以 ``k = anchor_factor``，
    **整段直接乘 anchor**。

    ## 为什么不做这一步就会毁掉所有图

    实测 000001：库里最新一根的因子是 **200.57**，按定义算出来只有 **1.0219** ——
    差 **196 倍**。两者**形状相同**（都是把分红加回去的后复权：腾讯 hfq 首末比
    1.1616 vs 不复权 0.9966，多出的 1.1656 ≈ 三年分红累积），**只是归一化锚点不同**
    （库里的锚在最早那根，我算的锚在最新那根）。

    因子是**乘在价格上**的，所以直接换表 ⇒ 每张 A 股图的每一个价格都会**缩放
    ~200 倍**。这不是精度问题，是量纲问题。

    ## 两版都错过，记下来

    - v1：只缩放台阶值、bar 里写死字面量 ``1.0`` ⇒ 最后一个除权日处凭空多出
      一个 7 倍假台阶（600519 暂存表最新 1.0 vs 库里 7.0605）；
    - v2：``k = anchor / fmap[max]`` ⇒ 缩放了两遍，最新一根变成 6.8973
      （应为 7.0605）。两次都是**看数字看出来的**，不是读代码看出来的。

    :returns: ``k``；拿不到锚点时返回 1.0（不重标定）。
    """
    if not steps or not anchor_factor:
        return 1.0
    return anchor_factor


def prev_closes_for(bars: tuple[tuple[int, float], ...], ex_dates: list[str]) -> dict[str, float]:
    """每个除权日对应的**除权前收盘**（往回找最近一个更早的交易日）。"""
    timeline = sorted(
        (dt.datetime.fromtimestamp(t / 1000, tz=dt.UTC).date().isoformat(), c) for t, c in bars
    )
    out: dict[str, float] = {}
    for ex in ex_dates:
        prior = [c for d, c in timeline if d < ex]
        if prior:
            out[ex] = prior[-1]
    return out


def process_code(
    client: AShareLocalClient,
    source: Any,
    code: str,
    *,
    write: bool,
    retries: int = 2,
) -> tuple[CodeResult, int]:
    """取一次公司行动、算因子、写暂存表。返回 ``(结果, 实际调用次数)``。

    ## 为什么要有界重试

    实测 Wind 这条通道**不稳**：本轮 5 次调用里 1 次在 90s 上限真超时
    （``subprocess.TimeoutExpired`` → ``WindSourceError``）。分几天无人值守跑时，
    一次超时就等于**浪费一格额度**还拿不到数据，所以瞬时失败要重试。

    但**额度/通道类错误绝不重试** —— 那必须立刻停下等下一天（见 :func:`main`）。

    ## R39：源可换

    ``source`` 现在是「任何带 ``fetch_corporate_actions`` 的对象」，并且**自带**
    异常分类（``fatal_errors`` / ``transient_errors``）—— 分类跟着源走，
    而不是写死成 Wind 的那几个类。默认源已从 Wind 换成东财（免费、无额度）。
    """
    conn = client._get_conn()  # noqa: SLF001
    fetch_code = AShareLocalClient._to_wind_code(code)  # noqa: SLF001

    calls = 0
    actions: tuple[Any, ...] = ()
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            actions = source.fetch_corporate_actions(fetch_code)
            calls += 1
            last_err = None
            break
        # ⚠️⚠️ 两个 except 的**顺序有语义**：transient 必须写在 fatal 前面。
        # 东财侧 `EastmoneyActionUnavailable`（网络）是 `EastmoneyActionError`
        # （解析）的**子类**；fatal 写在前面时，网络故障会被父类分支先吃掉
        # 并直接 raise，重试**永远不会发生**。R40 注入故障实测到的。
        except source.transient_errors as exc:  # 超时 / 后端错：值得重试
            calls += 1
            last_err = exc
            if attempt < retries:
                _LOG.warning("%s 第 %d 次取数失败（%s），重试", code, attempt + 1, exc)
                time.sleep(3.0 * (attempt + 1))
        except source.fatal_errors:
            raise  # 额度/确定性失败：交给 main 立刻停，不重试
    if last_err is not None:
        raise last_err

    bars = load_recent_closes(conn, code)
    if len(bars) < 30:
        return CodeResult(code, False, note="本地 K 线不足 30 根"), calls

    ex_dates = [a.ex_date for a in actions]
    first_bar = (
        dt.datetime.fromtimestamp(bars[0][0] / 1000, tz=dt.UTC).date().isoformat() if bars else ""
    )

    # ⚠️⚠️ R44 修：「从未在窗口内除权」不是失败，是**恒定因子**。
    #
    # 实测（2026-10-03，切表后仍占位的 177 只里）：
    #   99 只  从未分红            → 恒定，正确
    #   74 只  **全部**除权日 < 本地 bar 起点 2024-01-02 → 窗口内一次除权都没有
    #   2 只   新上市，daily_bar 只有 3 根 → 数据不足（下面 30 根那道门槛拦住）
    #
    # 这 74 只原来被「无任何可用除权台阶」整只拒写，是**过度保守**：
    # 除权日全在窗口之前 ⇒ 窗口内因子恒定，而恒定**就是正确答案**。
    # 拒写的代价是它永远留在占位状态，而且 ``failed`` 不进 ``done`` ⇒
    # 每天的定时重算都会把它重新查一遍（每天白烧 74 次东财调用，且永远失败）。
    #
    # ⚠️ 常数必须取 **anchor**（库里最新一根的值），不能取 1.0：
    # ``anchor_scale`` 在 steps 为空时返回 1.0（见其 docstring 的
    # ``if not steps ... return 1.0``），对占位票无所谓，但对**保留旧真值**的票
    # 会把 199.4 写成 1.0 ⇒ 显示价格缩放两个数量级。
    if not actions or all(d < first_bar for d in ex_dates):
        anchor = current_latest_factor(conn, code)
        # ⚠️ R45：这里原来对 `not actions` 直接 `return CodeResult(code, False, ...)` ——
        # **只记 note、不写任何行**，于是这些票永远留在占位状态。
        #
        # 而它**上面那段注释**恰恰写着这个代价有多坏：
        #     「拒写的代价是它永远留在占位状态，而且 ``failed`` 不进 ``done``
        #       ⇒ 每天的定时重算都会把它重新查一遍（每天白烧 74 次东财调用，
        #         且永远失败）」
        # —— 作者修好了 ``all(d < first_bar)`` 这一支（会写恒定行），
        # 却把**语义完全相同**的 ``not actions`` 这一支留在旧行为上。
        #
        # ## 为什么现在可以安全地写
        #
        # 因为**上游已经把「查无记录」和「接口故障」分开了**
        # （``cpt/adapters/eastmoney_actions.py``）：
        # 只有 ``_is_no_data`` 命中（东财明说 ``code=9201 返回数据为空``）
        # 才返回空元组；任何其它失败都 raise 成 ``EastmoneyActionError`` → fatal。
        # 它的判据写得很克制：「宁可漏判（退回 fatal、旧行为）也不误判
        # （把接口故障当成『没分过红』而静默写进暂存表）」。
        #
        # ⇒ 走到这一行时，``not actions`` 是**已确认的「没有公司行动」**，
        #    不是「不知道」。窗口内因子恒定**就是正确答案**，
        #    与 ``all(d < first_bar)`` 同理，应当写入。
        #
        # ⚠️ 常数必须取 **anchor**（库里最新一根的值），不能取 1.0 ——
        # 对「保留旧真值」的票，写 1.0 会让显示价格缩放两个数量级。
        const = anchor if anchor else 1.0
        detail = (
            f"{source.name}:events steps=0 (窗口内无除权，最早除权日早于 "
            f"bar 起点 {first_bar})"
            if actions
            else f"{source.name}:events steps=0 (东财确认无公司行动记录)"
        )
        rows = [
            (
                code,
                dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).date().isoformat(),
                const,
                f"{source.name}_events",
                detail,
            )
            for ms, _ in bars
        ]
        if write:
            save_recompute_factors(conn, rows)
        return CodeResult(code, True, rows=len(rows), steps=0,
                          note="无公司行动/因子恒定"), calls

    fmap = factor_from_actions(actions, prev_closes=prev_closes_for(bars, ex_dates))
    steps = sorted(fmap)
    if not steps:
        return CodeResult(code, False, note="无任何可用除权台阶（缺派息或除权前收盘）"), calls

    # ⚠️ 必须重标定：按定义算出的因子锚在**最新**一根，库里锚在**最早**一根
    # （实测 000001 相差 ~196 倍）。不重标定就直接换表 ⇒ 全部价格缩放两个数量级。
    # ⚠️⚠️ 缩放要作用在**整段序列**上，包括「最后一次除权之后」那些 bar。
    # 第一版只缩放了台阶值、而 bar 里写死字面量 ``1.0``，于是最后一个除权日
    # 处凭空多出一个 7 倍的假台阶（600519 暂存表最新 1.0 vs 库里 7.0605）。
    # 是"暂存表 vs 库里逐行对账"逮到的。
    anchor = current_latest_factor(conn, code)
    scale = anchor_scale(fmap, anchor)

    rows: list[tuple[str, str, float, str, str]] = []
    for open_ms, _close in bars:
        d = dt.datetime.fromtimestamp(open_ms / 1000, tz=dt.UTC).date().isoformat()
        # ⚠️ ``steps`` 已升序 ⇒ 要取**最近的那一次**除权（``later[0]``）的累计乘子。
        # 写成 ``later[-1]``（最晚那次）会让每一根 bar 都拿到同一个因子 ——
        # 整个分段结构塌成 2 个取值。是 R37 的形状对账（相对因子中位差 9%）逮到它的。
        # R40 修 off-by-one：原来取 fmap[later[0]]，是**反的**。
        #
        # factor_from_actions 从**最新**一次除权往前连乘，所以 fmap[s] = Π{ex >= s}。
        # 对一根 bar d，要的是「**已经发生**的那些事件」的乘积 Π{ex <= d} ——
        # 两者互补，而原代码取的是 Π{ex > d}，正好取反。
        #
        # 后果不是精度问题，是**方向**问题：除权日当天价格已向下跳，因子却还停在
        # 除权前的水平，于是后复权价在除权日**凭空多出一次下跌**。
        # 实测（600519 / 002614，四个除权日）：
        #     生产因子在除权日  1.0167 / 1.0265 / 1.0123 / 1.0196  向上，抵消除权
        #     原重算因子       0.9833 / 0.9769 / 0.9849 / 0.9808  向下，把除权放大
        # 独立判据是「后复权价在除权日应当连续」：生产侧消掉 43.3% 的跳空，
        # 原重算侧 -65.7%（把跳空放大）。是这项检验逮到它的，不是台阶幅度对账 ——
        # 后者当时 18/23 匹配，因为**幅度对、挂的日子错**。
        #
        # 取倒数正好还原「已发生事件的乘积」：
        #     f(d) = 1 / Π{ex > d} = 1 / fmap[min{ex > d}]
        # 最新一根（其后无事件）取 1.0 => 与 anchor_scale 的 k = anchor 自洽。
        later = [s for s in steps if s > d]
        factor = (1.0 / fmap[later[0]] if later else 1.0) * scale
        # 因子来源**跟着实际用的源写**，别再写死 "wind_events" ——
        # 东财算出来的行标成 wind 会让日后对账找不到出处（R39）。
        rows.append(
            (code, d, factor, f"{source.name}_events", f"{source.name}:events steps={len(steps)}")
        )
    if write:
        save_recompute_factors(conn, rows)
    return CodeResult(code, True, rows=len(rows), steps=len(steps)), calls


def report(state: dict[str, Any]) -> None:
    print("=== 进度 ===")
    print(f"  已完成 {len(state.get('done', []))} 只   失败 {len(state.get('failed', {}))} 只")
    days = state.get("days") or {}
    if days:
        print(f"  按日调用: {json.dumps(dict(sorted(days.items())), ensure_ascii=False)}")
    if state.get("stopped"):
        print(f"  上次停止: {state['stopped']}")
    failed = state.get("failed", {})
    if failed:
        print("  最近失败：")
        for code, why in list(failed.items())[-5:]:
            print(f"    {code}: {why}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按公司行动重算后复权因子（只写暂存表）")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help="本轮调用上限（Wind 侧真正的硬边界是 RATE_LIMIT_ERROR）",
    )
    parser.add_argument("--only", default="", help="只跑这些代码（逗号分隔，调试用）")
    parser.add_argument(
        "--scope",
        choices=SCOPES,
        default="placeholder",
        help="候选集：placeholder=只算从没算过的（默认，重算收益最大）/ "
        "all=所有有 bar 的票 / wired=只算已有真值的（旧行为，仅供对照）",
    )
    parser.add_argument("--report", action="store_true", help="只打印进度，不取数")
    parser.add_argument(
        "--retries", type=int, default=2, help="单只票的瞬时失败重试次数（额度类错误不重试）"
    )
    parser.add_argument(
        "--source",
        choices=("eastmoney", "wind"),
        default="eastmoney",
        help="公司行动真值源。默认 eastmoney：免费、从大阪直连 200、无额度；"
        "wind 保留作交叉校验（实测 24/2197 只就撞「账户积分余额不足」）",
    )
    parser.add_argument("--dry-run", action="store_true", help="照常取数与计算，但不写暂存表")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    state = load_state()
    if args.report:
        report(state)
        return 0

    client = AShareLocalClient()
    source: _Source = _WindSource() if args.source == "wind" else _EastmoneySource()
    _LOG.info("真值源 = %s", source.name)
    state = bind_state_to_source(state, source.name)
    # 候选集换了也要重开一轮：``done`` 的含义是「这只票**在某个 scope 下**算过」，
    # 从 placeholder 扩到 all 之后，先前算过的不代表全集都算过。
    if state.get("scope") != args.scope:
        if state.get("scope"):
            _LOG.warning(
                "候选集 scope 从 %s 变成 %s，进度**不继承**（done=%d 只将重跑）",
                state.get("scope"),
                args.scope,
                len(state.get("done") or []),
            )
        state = {
            "done": [],
            "failed": {},
            "days": {},
            "source": source.name,
            "scope": args.scope,
        }
    conn = client._get_conn()  # noqa: SLF001
    if not args.dry_run:
        ensure_recompute_stage(conn)

    if args.only:
        todo = [c.strip() for c in args.only.split(",") if c.strip()]
    else:
        done = set(state.get("done", []))
        todo = [c for c in priority_codes(conn, scope=args.scope) if c not in done]
        _LOG.info(
            "候选集 scope=%s：待处理 %d 只（已完成跳过 %d 只）", args.scope, len(todo), len(done)
        )

    today = dt.date.today().isoformat()
    calls = 0
    # ⚠️ R44：``state["stopped"]`` 会**留在进度文件里跨轮次**，所以不能用它判断
    # 「本轮是否被打断」—— 必须用这个只在本轮存活的局部标志。
    stopped_reason: str | None = None
    for code in todo:
        if calls >= args.max_calls:
            _LOG.info("到达本轮上限 %d 次调用，就此打住（下次续跑即可）", args.max_calls)
            break
        try:
            result, used = process_code(
                client, source, code, write=not args.dry_run, retries=args.retries
            )
        except source.fatal_errors as exc:
            _LOG.warning("额度/通道不可用（%s），本轮停止：%s", type(exc).__name__, exc)
            state["stopped"] = {"reason": type(exc).__name__, "detail": str(exc)[:200]}
            stopped_reason = type(exc).__name__
            save_state(state)
            break
        except Exception as exc:  # noqa: BLE001
            calls += 1
            _LOG.warning("%s 失败：%s: %s", code, type(exc).__name__, str(exc)[:160])
            state.setdefault("failed", {})[code] = f"{type(exc).__name__}: {str(exc)[:120]}"
            state.setdefault("days", {})[today] = calls
            save_state(state)
            continue
        calls += used  # 含重试消耗的额度
        if result.ok:
            state.setdefault("done", []).append(code)
            _LOG.info(
                "%s 完成：%d 行 / %d 个除权台阶%s",
                code,
                result.rows,
                result.steps,
                f"（{result.note}）" if result.note else "",
            )
        else:
            state.setdefault("failed", {})[code] = result.note
        state.setdefault("days", {})[today] = calls
        save_state(state)
        time.sleep(0.4)

    save_state(state)
    report(state)

    # ⚠️ R44：本轮**被打断**必须以非零码退出。
    #
    # 原来这里无条件 ``return 0``，于是 cron 里的
    # ``===== 开始 ===== / ===== 结束 rc=0 =====`` 把一次「启动 3 分钟就死」
    # 记录成一次**成功**的任务 —— 2026-10-02 13:47→13:50 那条日志就是这么来的。
    # rc=0 意味着任何监控（cron 邮件、CI、外部看门狗）都看不出它死了。
    #
    # 用 3 而不是 1：1 常被脚本当成「一般错误」，3 明确表示
    # 「任务未完成，进度已存盘，可续跑」，与「跑完了」区分得开。
    if stopped_reason:
        print(
            "\n  ⚠️ 本轮因 {} 提前停止（退出码 3）—— 进度已存盘，下次可续跑。".format(
                stopped_reason
            )
        )
        return 3

    # 本轮跑完 ⇒ 清掉上一轮遗留的 stopped 标记，否则它会永远挂在进度文件里，
    # 让「早就修好了」这件事在报告里看不出来。
    if state.get("stopped"):
        _LOG.info("本轮未被打断，清除上一轮的 stopped 标记")
        state.pop("stopped", None)
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
