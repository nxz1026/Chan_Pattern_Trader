"""东财公司行动源测试（R39/R44）。**全程离线**：注入假 ``opener``，绝不联网。

## 为什么这组测试的重量全压在「错误分类」上

R44 修的 bug 只有一个，但它杀的是**一整轮 3036 只**的任务：

    --scope placeholder 于 2026-10-02 13:50 启动，3 分钟后
    「上次因 EastmoneyActionError 提前停止：返回数据为空（001239）」

一只**从未分过红**的票把整轮掐死了。根因是「``success:false``」被无差别
当成 fatal，而东财恰恰用 ``success:false`` 表达「查无此记录」。

## 最重要的两条设计红线

1. **真·无数据 ⇒ 返回空行动列表**（不抛）。这���票「没分过红」是合法事实。
2. **接口故障 ≠ 无数据**。若把 9501（报表配置不存在）也当成「没分过红」，
   就会给一只**确实分过红**的票写一条恒为 1.0 的因子 —— 比掐停整轮**更糟**，
   因为它悄无声息地产出脏数据。所以 :func:`_is_no_data` 刻意保守：
   code + message + 空 result 三者同时成立才算数。

判据来自真机实测（2026-10-03 直连，非二手转述），独立数据源用新浪财经
``vISSUE_ShareBonus`` 交叉核对：001239 确有 3 条记录但方案全为「不分配」。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from cpt.adapters.eastmoney_actions import (
    EastmoneyActionClient,
    EastmoneyActionError,
    EastmoneyActionUnavailable,
    _is_no_data,
    normalize_code_for_em,
)

# ---------------------------------------------------------------------------
# 真机抓下来的原始 payload（2026-10-03）。**不要**把它们「简化」成 success:true
# 的形态 —— bug 恰恰只在 success:false 这一支上。
# ---------------------------------------------------------------------------

#: 001239 永贵电器：确有 3 条「分红」记录，但方案全是**不分配** ⇒ 真的没派过。
PAYLOAD_NO_DIVIDEND: dict[str, Any] = {
    "version": None,
    "result": None,
    "success": False,
    "message": "返回数据为空",
    "code": 9201,
}

#: 001238 浙江正特：正常返回（对照组）。
PAYLOAD_WITH_DIVIDEND: dict[str, Any] = {
    "version": "8aeb10cde1c9f0132bb9b4145afb1888",
    "result": {
        "pages": 1,
        "data": [
            {
                "SECURITY_CODE": "001238",
                "EX_DIVIDEND_DATE": "2026-06-03 00:00:00",
                "BONUS_IT_RATIO": 4,
                "BONUS_RATIO": None,
                "IT_RATIO": 4,
                "PRETAX_BONUS_RMB": 1,
                "ASSIGN_PROGRESS": "实施分配",
            }
        ],
    },
    "success": True,
    "code": 0,
}


def _client(payload: Any) -> EastmoneyActionClient:
    """把 ``payload`` 塞进 ``opener``，得到一个**离线**客户端。"""
    return EastmoneyActionClient(opener=lambda req, t: json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# 1. 无数据 = 合法结果（这条就是 R44 修的那条）
# ---------------------------------------------------------------------------


def test_no_dividend_returns_empty_instead_of_raising() -> None:
    """R44 回归：**核心**用例。

    修复前这里 raise ``EastmoneyActionError`` ⇒ 命中 ``_EastmoneySource.fatal_errors``
    ⇒ ``process_code`` 抛出 ⇒ ``main`` 停掉整轮。
    """
    actions = _client(PAYLOAD_NO_DIVIDEND).fetch_actions("001239")
    assert actions == ()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(PAYLOAD_NO_DIVIDEND, id="001239-real"),
        # 999999 是**不存在**的代码，东财同样回 9201 —— 「查无此票」与「无分红」
        # 在 API 层无法区分，而对本项目两者等价：都拿不到任何公司行动。
        pytest.param(
            {
                "version": None,
                "result": None,
                "success": False,
                "message": "返回数据为空",
                "code": 9201,
            },
            id="nonexistent-code",
        ),
        # 万一东财哪天把 result 改成 {"data": null} / {"data": []}，仍应算无数据。
        pytest.param(
            {"result": {"data": None}, "success": False, "message": "返回数据为空", "code": 9201},
            id="data-null",
        ),
        pytest.param(
            {"result": {"data": []}, "success": False, "message": "返回数据为空", "code": 9201},
            id="data-empty",
        ),
    ],
)
def test_legit_no_data_shapes(payload: dict[str, Any]) -> None:
    assert _client(payload).fetch_actions("001239") == ()


def test_success_true_with_empty_data_is_empty() -> None:
    payload = {"result": {"data": []}, "success": True, "code": 0}
    assert _client(payload).fetch_actions("600519") == ()


# ---------------------------------------------------------------------------
# 2. 接口故障必须**仍然**是 fatal（这条比 §1 更要紧）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        pytest.param(
            {
                "version": None,
                "result": None,
                "success": False,
                "message": "报表配置不存在,RPT_NONEXISTENT_XYZ",
                "code": 9501,
            },
            "9501=请求写错了（重试无用），绝不能当成「这家公司没分过红」",
            id="bad-report-name-9501",
        ),
        pytest.param(
            {
                "result": {"data": [{"EX_DIVIDEND_DATE": "2024-01-01"}]},
                "success": False,
                "message": "返回数据为空",
                "code": 9201,
            },
            "9201 却带**非空** result ⇒ 自相矛盾，按 fatal 更安全",
            id="9201-with-nonempty-result",
        ),
        pytest.param(
            {"result": None, "success": False, "message": "系统繁忙", "code": 9201},
            "9201 但 message 不对 ⇒ 不能只认 code",
            id="9201-with-unknown-message",
        ),
    ],
)
def test_broken_or_ambiguous_payload_stays_fatal(payload: dict[str, Any], why: str) -> None:
    with pytest.raises(EastmoneyActionError):
        _client(payload).fetch_actions("600519")
    assert why  # 让 pytest 参数化报告里带上判读理由


def test_non_json_is_fatal() -> None:
    """HTML 错误页（反代 502 等）—— 重试只会再拿一次同样的坏数据。"""
    client = EastmoneyActionClient(opener=lambda req, t: b"<html>502 Bad Gateway</html>")
    with pytest.raises(EastmoneyActionError):
        client.fetch_actions("600519")


def test_unavailable_is_a_subclass_of_error() -> None:
    """R40 的教训由**类型层次**兜底：网络类必须是解析类的子类。

    ``process_code`` 里 transient 的 ``except`` 排在 fatal 前面，子类才会
    先被 transient 抓到并重试。顺序写反 ⇒ 重试永不发生。
    """
    assert issubclass(EastmoneyActionUnavailable, EastmoneyActionError)


def test_network_error_is_unavailable_not_plain_error() -> None:
    """网络不可达要归 transient（值得重试），不能归 fatal。"""

    def boom(req: Any, t: float) -> bytes:
        raise OSError("Connection reset by peer")

    with pytest.raises(EastmoneyActionUnavailable):
        EastmoneyActionClient(opener=boom).fetch_actions("600519")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([1, 2, 3], id="top-level-list"),
        pytest.param({"result": [1, 2, 3], "success": True, "code": 0}, id="result-not-dict"),
        pytest.param({"result": {"data": "oops"}, "success": True, "code": 0}, id="data-not-list"),
    ],
)
def test_structurally_corrupt_payload_is_fatal(payload: Any) -> None:
    """结构损坏要**明确报错**，而不是在 ``.get`` 上抛一个难懂的 AttributeError。

    R44 顺带加固：原代码 ``(payload.get("result") or {}).get("data")`` 在
    ``result`` 是 list 时会抛 ``AttributeError``，那个异常既不属于
    ``fatal_errors`` 也不属于 ``transient_errors``，会被 main 的兜底分支
    当成「这只票失败」—— 一只票坏数据，静悄悄混进 failed 列表。
    """
    with pytest.raises(EastmoneyActionError):
        _client(payload).fetch_actions("600519")


# ---------------------------------------------------------------------------
# 3. 正常路径没被改坏
# ---------------------------------------------------------------------------


def test_normal_dividend_still_parsed() -> None:
    actions = _client(PAYLOAD_WITH_DIVIDEND).fetch_actions("001238")
    assert len(actions) == 1
    a = actions[0]
    assert a.ex_date == "2026-06-03"
    # 每 10 股 -> 每股
    assert a.cash_pre_tax == pytest.approx(0.1)
    # BONUS_IT_RATIO 是送+转**总和**（R41 修的那个一倍 bug），不得再加 IT_RATIO
    assert a.share_bonus == pytest.approx(0.4)
    assert a.transfer is None
    assert a.source == "eastmoney"


def test_unimplemented_plan_is_filtered_out() -> None:
    """未实施的方案不能用 —— 否则等于按还没发生的事件调价。"""
    payload = {
        "result": {
            "data": [
                {
                    "EX_DIVIDEND_DATE": "2026-06-03 00:00:00",
                    "BONUS_IT_RATIO": 4,
                    "PRETAX_BONUS_RMB": 1,
                    "ASSIGN_PROGRESS": "预案",
                }
            ]
        },
        "success": True,
        "code": 0,
    }
    assert _client(payload).fetch_actions("001238") == ()


@pytest.mark.parametrize(("raw", "want"), [("600519", "600519"), ("600519.SH", "600519")])
def test_normalize_code(raw: str, want: str) -> None:
    assert normalize_code_for_em(raw) == want


# ---------------------------------------------------------------------------
# 4. _is_no_data 的判定矩阵（把「保守」这个设计选择钉成契约）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "want"),
    [
        pytest.param(PAYLOAD_NO_DIVIDEND, True, id="9201+返回数据为空+空result"),
        pytest.param({"code": 9201, "message": "返回数据为空", "result": None}, True, id="minimal"),
        pytest.param({"code": 9201, "message": "暂无数据", "result": None}, True, id="暂无数据"),
        pytest.param(
            {"code": 9501, "message": "返回数据为空", "result": None}, False, id="code-mismatch"
        ),
        pytest.param(
            {"code": 9201, "message": "系统繁忙", "result": None}, False, id="msg-mismatch"
        ),
        pytest.param(
            {"code": 9201, "message": "返回数据为空", "result": {"data": [1]}},
            False,
            id="nonempty-result",
        ),
        pytest.param({"message": "返回数据为空", "result": None}, False, id="missing-code"),
    ],
)
def test_is_no_data_matrix(payload: dict[str, Any], want: bool) -> None:
    assert _is_no_data(payload, str(payload.get("message") or "")) is want


# ---------------------------------------------------------------------------
# data 里的非对象行（R59 审计 L8）
#
# 改前 ``row.get(...)`` 直接抛 ``AttributeError`` —— 不是 ``EastmoneyActionError``，
# 上层按领域异常接不住，一条字符串就把整只票（乃至整轮任务）的公司行动全部丢掉。
# ---------------------------------------------------------------------------


def test_non_object_rows_are_skipped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    good = dict(PAYLOAD_WITH_DIVIDEND["result"]["data"][0])
    payload: dict[str, Any] = {
        "version": PAYLOAD_WITH_DIVIDEND["version"],
        "result": {"pages": 1, "data": ["oops", 42, good]},
        "success": True,
        "code": 0,
    }
    with caplog.at_level(logging.WARNING, logger="cpt.adapters.eastmoney_actions"):
        actions = _client(payload).fetch_actions("001238")
    assert len(actions) == 1, "坏行跳过，好行照常解析"
    assert actions[0].source == "eastmoney"
    assert any("不是 JSON 对象" in record.getMessage() for record in caplog.records), (
        "跳过坏行必须留可观测痕迹"
    )
