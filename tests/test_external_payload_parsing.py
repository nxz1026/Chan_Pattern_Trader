"""外部数据源的**载荷解析**（R45 P0-1 补测 · 第五批：外部接口）。

## 补什么

覆盖率报告里最低的两个纯函数：

- ``binance_futures._http_error_detail``（**15.4%**）——
  从 Binance 错误响应体里提取 ``code``/``msg``。
- ``wind_source.parse_corporate_actions``（**42.9%**）——
  归一化 Wind 的公司行动返回体。

## 为什么它们值得测

两者都是**纯函数**（无 DB、无网络），覆盖率低**纯粹是因为没人写**，
不是因为难测。而它们都在**数据入口**上：

- 错误信息解析错 ⇒ 排查时看到的是无意义的 ``body=b'...'``；
- 公司行动解析错 ⇒ 因子算错，**而且静默**（R45 一整天都在对付这类静默）。

Wind 那条 docstring 写得很关键：「**列集合按标的、甚至按次而变**」
⇒ 解析必须按**列名子串**、不按下标。这类「形状会变的输入」正是
最需要用测试钉住契约的地方。

全程离线。
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.adapters.binance_futures import _http_error_detail  # noqa: E402
from cpt.adapters.wind_source import parse_corporate_actions  # noqa: E402


def _http_error(body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", 400, "Bad Request", {}, None)  # type: ignore[arg-type]


class _Resp:
    def __init__(self, payload: bytes | Exception) -> None:
        self._payload = payload

    def read(self) -> bytes:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _err(body: bytes | Exception) -> urllib.error.HTTPError:
    e = urllib.error.HTTPError("https://x", 400, "Bad Request", {}, None)  # type: ignore[arg-type]
    e.read = lambda: _Resp(body).read()  # type: ignore[method-assign]
    return e


# ── _http_error_detail ────────────────────────────────────────
def test_extracts_code_and_msg() -> None:
    """标准错误体 ⇒ 拿到 code/msg（而不是一坨 body）。"""
    out = _http_error_detail(_err(json.dumps({"code": -1121, "msg": "Invalid symbol"}).encode()))
    assert "-1121" in out and "Invalid symbol" in out


def test_non_json_body_is_truncated_not_crashed() -> None:
    """HTML/纯文本错误页 ⇒ 降级成 ``body=...``，且**截断**到 200 字符。"""
    out = _http_error_detail(_err(b"<html>" + b"x" * 500))
    assert out.startswith("body=")
    assert len(out) < 260, f"没截断，长度 {len(out)}"


def test_read_failure_degrades_to_empty() -> None:
    """**读都读不到** ⇒ 返回空串，不抛。

    错误处理里再抛一次会把「原始错误」盖掉，变成更难查的异常。
    """
    assert _http_error_detail(_err(OSError("stream closed"))) == ""


def test_json_array_body_is_not_misparsed() -> None:
    """合法 JSON 但是**数组** ⇒ 走 body 分支（不是 Mapping）。"""
    out = _http_error_detail(_err(b"[1,2,3]"))
    assert out.startswith("body="), out


# ── parse_corporate_actions ──────────────────────────────────
# ⚠️ 第一版我把 Wind 信封编错了三处，全是**我以为的形状**：
#   1. ``columns`` 是 **含 name 的 dict 列表**，不是字符串列表；
#   2. 行在 ``rows`` 下，不是 ``data``；
#   3. 字段叫 ``送股``/``转增``（CorporateAction 也没有 share_ratio）；
#   4. 而且它**会按 ex_date 排序** —— ���「保持输入顺序」是我编的。
def _wind(columns: list[str], rows: list[list]) -> dict:
    """按 **2026-10-02 实测的真实信封**造：``data.data`` 是**列表**。

    ⚠️ 我第一版写成 ``{"data": {"tables": [表]}}`` ⇒ ``_find_table`` 找不到 ⇒ 0 条。
    而 ``_find_table`` 的 docstring 恰好记着这个坑：
    「第一版按 ``data["data"]`` 取，拿到的是 **dict 不是 list**，
    于是整表被当成『无记录』—— 表现为『Wind 无公司行动记录』，
    一个**看起来像数据缺失**的错误。」
    ⇒ 造的夹具必须是**当时那个真实形状**，不能是我想象的。
    """
    return {"data": {"data": [{
        "columns": [{"name": c} for c in columns],
        "rows": rows,
    }], "error": None}, "error": None}


def test_empty_envelope_yields_empty_tuple() -> None:
    assert parse_corporate_actions({}) == ()
    assert parse_corporate_actions({"data": None}) == ()
    assert parse_corporate_actions({"data": {"data": []}}) == ()
    # 递归深度上限：套 8 层也认不出来 ⇒ 返回空而不是 RecursionError
    deep = {"columns": [{"name": "除权除息日"}], "rows": [["2026-06-10"]]}
    for _ in range(8):
        deep = {"data": deep}
    assert parse_corporate_actions(deep) == ()


def test_maps_by_column_name_not_index() -> None:
    """⚠️ **列顺序变了也不影响** —— docstring 明说「列集合按标的甚至按次而变」，
    解析全程用列名子串。第一版我把列顺序打乱就该照样解析出来。"""
    acts = parse_corporate_actions(_wind(
        ["转增比例", "代码", "除权除息日", "税前每股派息"],
        [[0.1, "000001.SZ", "2026-06-10", 0.5]],
    ))
    assert len(acts) == 1
    a = acts[0]
    assert a.ex_date == "2026-06-10"
    assert a.cash_pre_tax == pytest.approx(0.5)
    assert a.transfer == pytest.approx(0.1)


def test_missing_optional_column_is_tolerated() -> None:
    """少一列（docstring 说「两次调用分别少了/多了某列」）⇒ **不能抛**。"""
    acts = parse_corporate_actions(_wind(
        ["除权除息日", "税前每股派息"], [["2026-06-10", 0.3]]
    ))
    assert len(acts) == 1
    assert acts[0].cash_pre_tax == pytest.approx(0.3)
    assert acts[0].transfer is None or acts[0].transfer == pytest.approx(0.0)


def test_sorts_by_ex_date_not_input_order() -> None:
    """⚠️ 实现**按 ex_date 排序**（不是保持输入顺序）——
    我第一版断言「保持输入顺序」是编的。因子计算依赖时间序，
    所以这个排序是**必需**的，测试要钉的是它。"""
    acts = parse_corporate_actions(_wind(
        ["除权除息日", "税前每股派息"],
        [["2026-12-01", 0.1], ["2026-01-01", 0.2], ["2026-06-01", 0.3]],
    ))
    assert [a.ex_date for a in acts] == ["2026-01-01", "2026-06-01", "2026-12-01"]


def test_row_without_usable_date_is_skipped() -> None:
    """没有 / 日期太短的行 ⇒ 跳过（**不能**造一个 1970 的日期出来）。"""
    acts = parse_corporate_actions(_wind(
        ["除权除息日", "税前每股派息"],
        [[None, 0.3], ["", 0.4], ["2026", 0.5], ["2026-06-10", 0.6]],
    ))
    assert [a.ex_date for a in acts] == ["2026-06-10"]


def test_date_with_slashes_is_normalised() -> None:
    """``2026/09/24`` ⇒ ``2026-09-24``。"""
    acts = parse_corporate_actions(_wind(
        ["除权除息日", "税前每股派息"], [["2026/09/24", 0.3]]
    ))
    assert acts[0].ex_date == "2026-09-24"


def test_ragged_row_is_skipped_not_crashed() -> None:
    """行比列短 ⇒ 跳过（不 IndexError）。"""
    acts = parse_corporate_actions(_wind(
        ["除权除息日", "税前每股派息"], [["2026-06-10"]]
    ))
    assert len(acts) == 1
    assert acts[0].cash_pre_tax is None
