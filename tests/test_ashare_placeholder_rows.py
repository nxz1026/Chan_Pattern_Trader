"""A 股占位行（O/H/L 全 0）不得污染序列，且必须**响亮**（R52）。

**离线**：全 fake client，不连 DB。

## 这个问题为什么值得单独立测试

生产实测（2026-10-05 查库）：`public.daily_bar` 在 2026-09-28~09-30 三天里
一次批量写出 **35 行 `open=high=low=0` 且 `vol=amt=0`** 的废行，横跨 **18 只票**。
危害分两档，**第二档从没有任何门禁发现过**：

- `close≠0`（上游填了前收盘价，17 行）：校验抛 ``DataValidationError``
  ⇒ **整只票降级**，657 根里 1 根坏就全废。
- `close=0`（完全空行，18 行）：``0<=0<=0`` 成立 ⇒ 校验器**放行**
  ⇒ 零价 K 线进结构计算 ⇒ 假分型/假笔/假中枢，**全程零报错**。

第二档是这轮真正要杀的目标：它不产生**任何**失败信号，画布上只是悄悄多一根
0 价针，用户看不出、门禁也抓不到。修法落在 adapters（丢弃），理由与取舍见
``cpt/adapters/a_share_local.py::fetch_validated_klines`` 内的注释。

本文件钉住两件容易被后人「优化掉」的事：

1. **丢弃**（在 ``test_a_share_local.py`` 钉适配器行为，这里钉应用层行为）；
2. **响亮** —— 丢弃不能是静默的，否则下次批量坏行时没人知道，排查会退回
   「随机少一天，查不出原因」。这是本仓的既有纪律（见
   ``test_ashare_db_error_not_masked.py``：DB 故障不得被报成「缺因子」）。
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.adapters.a_share_local import ASharePlaceholderRowsError  # noqa: E402
from cpt.application import a_share_snapshot as snap  # noqa: E402
from cpt.domain.models import CanonicalBar  # noqa: E402

DAY_MS = 24 * 3600 * 1000


def _bars(n: int) -> tuple[CanonicalBar, ...]:
    """n 根合法日线（收盘价在 10 元上下小步波动），时间轴是连续日历日。

    连续日历日是**故意**的：``validate_ashare_bars`` 允许 A 股的日历缺口，
    所以不必构造交易日历；要防的只有「非交易日不出图」被 0 填充破坏这一点。
    """
    start = datetime(2026, 6, 1, tzinfo=UTC)
    out = []
    for i in range(n):
        base = 10.0 + (i % 7) * 0.1
        open_ms = int((start + timedelta(days=i)).timestamp() * 1000)
        out.append(
            CanonicalBar(
                open_time=open_ms,
                open=base,
                high=base + 0.3,
                low=base - 0.3,
                close=base + 0.1,
                volume=1000.0 + i,
                close_time=open_ms + DAY_MS - 1,
                quote_volume=2000.0 + i,
                trade_count=0,
                taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0,
                is_closed=True,
            )
        )
    return tuple(out)


class _PlaceholderClient:
    """返回「N 根好 bar + 几行被丢弃的占位行」的假客户端。"""

    def __init__(self, *, bars: int = 200, placeholders: tuple[str, ...] = ("2026-09-28",)):
        self._result = type(
            "R",
            (),
            {
                "bars": _bars(bars),
                "skipped_no_factor": (),
                "skipped_placeholder": placeholders,
            },
        )()

    def fetch_validated_klines(self, *a: Any, **k: Any) -> Any:
        return self._result

    def _get_conn(self) -> Any:
        return None

    def close(self) -> None:
        pass


class _AllPlaceholderClient:
    """区间内**每一行**都是占位行 ⇒ 适配器抛专属错误。"""

    def __init__(self) -> None:
        self._conn = None

    def fetch_validated_klines(self, *a: Any, **k: Any) -> Any:
        raise ASharePlaceholderRowsError("000002 区间内 3 行全是占位行（O/H/L 全 0、无成交）")

    def _get_conn(self) -> Any:
        return self._conn

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. 部分占位行：序列照常产出，但必须留 WARNING
# ---------------------------------------------------------------------------


def test_partial_placeholder_rows_still_yield_a_snapshot() -> None:
    """丢弃占位行后，这只票**不该**降级 —— 200 根里 2 根缺不影响结构。

    成功路径的 ``data_quality`` 与降级路径**形状不同**：降级才有 ``degraded``/
    ``reason``，成功只有 ``severity``。断言按各自真实形状写，不要想当然。
    """
    client = _PlaceholderClient(placeholders=("2026-09-28", "2026-09-29"))
    out = snap.build_ashare_snapshot("600519", client=client, ensure_factors=None)  # type: ignore[arg-type]
    assert len(out["candles"]) == 200
    assert out["data_quality"]["severity"] == "ok"
    assert out["runtime"]["status"] != "empty"
    # 降级路径才有的键，成功时不该出现
    assert "reason" not in out["data_quality"]


def test_partial_placeholder_rows_are_announced_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """**这是本文件的核心断言**：静默丢弃等于埋雷。

    上游废行是**会重复发生**的（实测一次批量 18 只票）。如果丢弃不打日志，
    下次批量坏行时没有任何信号，排查会退回「随机少一天，查不出原因」——
    这正是本仓吃过亏的那类问题（判据失效且无声）。
    """
    client = _PlaceholderClient(placeholders=("2026-09-28", "2026-09-29"))
    with caplog.at_level(logging.WARNING, logger="cpt.application.a_share_snapshot"):
        snap.build_ashare_snapshot("600519", client=client, ensure_factors=None)  # type: ignore[arg-type]
    warned = [r for r in caplog.records if "占位行" in r.getMessage()]
    assert warned, "占位行被静默丢弃了 —— 上游写出废行这件事没人会知道"
    msg = warned[0].getMessage()
    assert "600519" in msg
    assert "2" in msg, "日志必须带上丢弃的**行数**，否则判断不了影响面"
    assert "2026-09-28" in msg, "日志必须带样例日期，否则没法去 DB 里核对"


def test_no_warning_when_there_are_no_placeholder_rows(caplog: pytest.LogCaptureFixture) -> None:
    """对照组：没有占位行时不得刷 WARNING（否则这条日志会被当成噪音忽略）。"""
    client = _PlaceholderClient(placeholders=())
    with caplog.at_level(logging.WARNING, logger="cpt.application.a_share_snapshot"):
        snap.build_ashare_snapshot("600519", client=client, ensure_factors=None)  # type: ignore[arg-type]
    assert not [r for r in caplog.records if "占位行" in r.getMessage()]


# ---------------------------------------------------------------------------
# 2. 全部占位行：报专属 reason，不许混进 no_factor / no_data / db_error
# ---------------------------------------------------------------------------


def test_all_placeholder_rows_report_placeholder_rows_reason() -> None:
    client = _AllPlaceholderClient()
    out = snap.build_ashare_snapshot("600519", client=client, ensure_factors=None)  # type: ignore[arg-type]
    reason = out["data_quality"]["reason"]
    assert reason == "placeholder_rows", (
        f"占位行故障被报成 {reason!r} —— 排查会被指到因子表或采集连通性，而真凶是『采集写出了废行』"
    )
    assert out["data_quality"]["degraded"] is True
    assert out["runtime"]["status"] == "empty"


def test_all_placeholder_rows_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    client = _AllPlaceholderClient()
    with caplog.at_level(logging.WARNING, logger="cpt.application.a_share_snapshot"):
        snap.build_ashare_snapshot("600519", client=client, ensure_factors=None)  # type: ignore[arg-type]
    assert [r for r in caplog.records if "占位行" in r.getMessage()]


def test_placeholder_rows_reason_is_not_one_of_the_old_vocabulary() -> None:
    """钉住「新增了词」这件事本身 —— 防止有人为省事回退到 no_data。

    本仓的 reason 词表就是排查字典（``empty_ashare_snapshot`` docstring 明说
    「reason 是给用户看的，必须具体」）。占位行是一个**新的**上游故障类型，
    复用旧词等于把新问题藏进旧分类。
    """
    client = _AllPlaceholderClient()
    out = snap.build_ashare_snapshot("600519", client=client, ensure_factors=None)  # type: ignore[arg-type]
    reason = out["data_quality"]["reason"]
    assert reason not in {"no_factor", "no_data"}
    assert not reason.startswith(("db_error", "invalid_bars"))


# ---------------------------------------------------------------------------
# 3. duck-type 兼容：老式假结果对象只有两个字段，不得崩
# ---------------------------------------------------------------------------


def test_legacy_two_field_result_object_still_works() -> None:
    """大量测试/桩只给 ``bars`` + ``skipped_no_factor``。新增字段不得成为硬要求。"""

    class _LegacyClient:
        def fetch_validated_klines(self, *a: Any, **k: Any) -> Any:
            return type("R", (), {"bars": _bars(200), "skipped_no_factor": ()})()

        def _get_conn(self) -> Any:
            return None

        def close(self) -> None:
            pass

    out = snap.build_ashare_snapshot("600519", client=_LegacyClient(), ensure_factors=None)  # type: ignore[arg-type]
    assert len(out["candles"]) == 200
    assert out["data_quality"]["severity"] == "ok"


def test_skipped_placeholder_helper_is_duck_type_safe() -> None:
    assert snap._skipped_placeholder(type("R", (), {})()) == ()
    assert snap._skipped_placeholder(type("R", (), {"skipped_placeholder": None})()) == ()
    assert snap._skipped_placeholder(type("R", (), {"skipped_placeholder": ("x",)})()) == ("x",)
