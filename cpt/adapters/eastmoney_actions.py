"""东财「分红送配」公司行动源（**免费、可从大阪直连**）。

## 为什么需要它（R39）

R37 的重算原打算以 Wind 为唯一真值，结果真跑起来第一件事就撞墙：

    WindQuotaError: Wind 额度/限流（backend_error）：**账户积分余额不足**
    24/2197 只就停了。

而真值不必花钱：``datacenter-web.eastmoney.com`` 的
``RPT_SHAREBONUS_DET`` 从大阪**直连 200**（实测 2026-10-02），字段齐全：

===========================  ==========================================
``EX_DIVIDEND_DATE``          除权除息日
``PRETAX_BONUS_RMB``          税前派息，⚠️ **每 10 股**（见 corporate_actions）
``BONUS_IT_RATIO``            送股比例（每 10 股）
``IT_RATIO`` / ``BONUS_RATIO`` 转增比例
``ASSIGN_PROGRESS``           实施进度（未实施的**不能用**）
===========================  ==========================================

对比 Wind：同样的字段、同样的语义，**不消耗任何额度**，也不受 Wind 那 90s
超时与积分波动影响。Wind 保留为可选的交叉校验源。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

from cpt.adapters.corporate_actions import (
    CorporateAction,
    _as_float,
    ex_div_ratio,
    filter_implemented,
)

_LOG = logging.getLogger(__name__)

__all__ = [
    "EASTMONEY_BONUS_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "EastmoneyActionClient",
    "EastmoneyActionError",
    "EastmoneyActionUnavailable",
    "normalize_code_for_em",
]

EASTMONEY_BONUS_URL: Final[str] = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0

#: 一次拉多少条历史（600519 全史 28 条；给 200 足够覆盖所有 A 股）
_PAGE_SIZE: Final[int] = 200

_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

#: 送转比例的列名候选（实测这两个源在不同标的上给不同的列）
_BONUS_COLS: Final[tuple[str, ...]] = ("BONUS_IT_RATIO", "BONUS_RATIO")
_TRANSFER_COLS: Final[tuple[str, ...]] = ("IT_RATIO", "TRANSFER_IT_RATIO", "TRANSFER_RATIO")


def normalize_code_for_em(code: str) -> str:
    """``600519`` / ``600519.SH`` → ``600519``（东财只要 6 位）。"""
    return code.split(".")[0][:6]


def _first_value(row: dict[str, Any], cols: tuple[str, ...]) -> Any:
    for c in cols:
        if c in row:
            return row[c]
    return None


def _parse_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) < 10:
        return None
    return text[:10].replace("/", "-")


class EastmoneyActionClient:
    """按 6 位代码取全部公司行动（已过滤为**已实施**）。"""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Any = None,
    ) -> None:
        self._timeout = timeout
        self._opener = opener

    def fetch_actions(self, code: str) -> tuple[CorporateAction, ...]:
        """取该代码的公司行动；**无记录返回空元组**（不抛）。

        Raises:
            EastmoneyActionError: HTTP/网络/JSON 层面的失败（区别于"这家没分红"）。
        """
        em_code = normalize_code_for_em(code)
        params = {
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{em_code}")',
            "pageSize": str(_PAGE_SIZE),
            "sortColumns": "EX_DIVIDEND_DATE",
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        }
        url = f"{EASTMONEY_BONUS_URL}?{urllib.parse.urlencode(params)}"

        def _default_open(req: urllib.request.Request, t: float) -> bytes:
            with urllib.request.urlopen(req, timeout=t) as resp:  # noqa: S310
                return bytes(resp.read())

        send = self._opener if self._opener is not None else _default_open
        try:
            raw = send(urllib.request.Request(url, headers=_HEADERS), self._timeout)  # noqa: S310
        except (urllib.error.URLError, OSError) as exc:
            # ⚠️ 网络不可达与「返回坏数据」是**两件事**，必须分开：
            # 读超时是**瞬时**的（重试有意义），返回非 JSON 是**确定性**的
            # （重试只会再拿一次同样的坏数据）。R40 实测：把两者混成同一个
            # 异常类、且一律当「不重试」，一次 15s 读超时就把 2189 只的
            # 整轮批次掐停了 —— 而那条其实重试一次就成了。
            raise EastmoneyActionUnavailable(f"东财分红接口不可达 {em_code}：{exc}") from exc

        try:
            payload = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
        except (json.JSONDecodeError, AttributeError) as exc:
            raise EastmoneyActionError(
                f"东财分红接口返回非 JSON（{em_code}）：{str(raw)[:120]}"
            ) from exc

        if payload.get("success") is False:
            raise EastmoneyActionError(
                f"东财分红接口返回失败（{em_code}）：{payload.get('message')}"
            )
        rows = ((payload.get("result") or {}).get("data")) or []
        out: list[CorporateAction] = []
        for row in rows:
            ex = _parse_date(row.get("EX_DIVIDEND_DATE"))
            if ex is None:
                continue  # 没有除权日 = 还没到实施，定位不到台阶
            cash10 = _as_float(row.get("PRETAX_BONUS_RMB"))
            bonus = _as_float(_first_value(row, _BONUS_COLS))
            transfer = _as_float(_first_value(row, _TRANSFER_COLS))
            out.append(
                CorporateAction(
                    ex_date=ex,
                    # ⚠️ 每 10 股 → 每股；送转同理（那两列也是每 10 股的比例）
                    cash_pre_tax=None if cash10 is None else ex_div_ratio(cash10),
                    share_bonus=None if bonus is None else ex_div_ratio(bonus),
                    transfer=None if transfer is None else ex_div_ratio(transfer),
                    source="eastmoney",
                    status=str(row.get("ASSIGN_PROGRESS") or ""),
                )
            )
        return filter_implemented(out)


class EastmoneyActionError(RuntimeError):
    """东财分红接口**解析**失败（与"这家没有分红"是两件事）。

    这是**确定性**失败：同样的请求再发一次，还是同样的坏数据，
    所以调用方不该重试。
    """


class EastmoneyActionUnavailable(EastmoneyActionError):
    """东财分红接口**网络不可达**（读超时 / 连接失败 / DNS 失败）。

    与 :class:`EastmoneyActionError` 的区别就是**该不该重试**：网络故障是
    瞬时的，隔几秒再试很可能就成了。R40 实测一次 15s 读超时让整轮 2189 只
    的批次直接停止，而那一次重试本可以拿到数据。
    """
