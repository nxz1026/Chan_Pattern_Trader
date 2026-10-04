"""看板自选列表的**信号状态列**（R45 P0-1 · 第七批：用户可见）。

## 为什么补这个

``app._latest_signal_statuses`` 覆盖率 **38.5%**（16/26 行未覆盖）。
它填的是**看板上每只票那一行**的 ``signal_status`` —— 用户天天看得到。

## 钉什么

1. **首次出现即最新** —— ``load_signal_events`` 返回**时间倒序**，
   所以「第一次看到某个 code」就是它的最新状态。
   搞反了会让**每一行都显示历史状态**，而且看起来完全正常。
2. 缺 ``code`` / ``code`` 不是字符串的行 ⇒ **跳过**（不能把 None 当 code）。
3. 缺 ``status`` ⇒ 填 ``"none"``（诚实缺省，不是空串）。
4. 读失败 ⇒ 返回**空 dict** 且**留 warning**。
   ⚠️ 空 dict 是诚实的：调用方会把这一列填成 ``"none"``，
   「没读到」不会伪装成别的结论。但如果**静默**返回空 dict，
   排查的人就看不出是 DB 挂了还是真的没信号。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.web import app as web_app  # noqa: E402


def _patch(monkeypatch, events, *, exc: Exception | None = None, closed: list | None = None):
    import cpt.adapters.a_share_local as local
    import cpt.storage.signal_event_store as store

    class _C:
        def _get_conn(self) -> str:  # noqa: ANN202
            return "CONN"

        def close(self) -> None:
            if closed is not None:
                closed.append(True)

    monkeypatch.setattr(local, "AShareLocalClient", _C)

    def _load(*a, **k):  # noqa: ANN001, ANN202
        if exc:
            raise exc
        return list(events)

    monkeypatch.setattr(store, "load_signal_events", _load)


def test_first_occurrence_wins(monkeypatch) -> None:
    """⚠️ **倒序输入，首次出现即最新** —— 搞反会让每行显示历史状态且看不出。"""
    _patch(monkeypatch, [
        {"code": "600519", "status": "invalidated"},   # 最新
        {"code": "600519", "status": "confirmed"},     # 更早
        {"code": "000001", "status": "structure_ready"},
    ])
    out = web_app._latest_signal_statuses()  # type: ignore[attr-defined]  # noqa: SLF001
    assert out == {"600519": "invalidated", "000001": "structure_ready"}, out


def test_skips_rows_without_usable_code(monkeypatch) -> None:
    """缺 code / code 非字符串 ⇒ 跳过（**不能**拿 None 当 key）。"""
    _patch(monkeypatch, [
        {"status": "confirmed"},                  # 无 code
        {"code": None, "status": "confirmed"},    # code 是 None
        {"code": 600519, "status": "confirmed"},  # code 是 int 不是 str
        {"code": "000001", "status": "confirmed"},
    ])
    out = web_app._latest_signal_statuses()  # type: ignore[attr-defined]  # noqa: SLF001
    assert out == {"000001": "confirmed"}, out


def test_missing_status_becomes_none_string(monkeypatch) -> None:
    """缺 status ⇒ ``"none"``（诚实缺省，不是空串 —— 空串在前端会显示成空白）。"""
    _patch(monkeypatch, [{"code": "000002"}])
    out = web_app._latest_signal_statuses()  # type: ignore[attr-defined]  # noqa: SLF001
    assert out == {"000002": "none"}, out


def test_empty_events_yields_empty_dict(monkeypatch) -> None:
    _patch(monkeypatch, [])
    assert web_app._latest_signal_statuses() == {}  # type: ignore[attr-defined]  # noqa: SLF001


def test_read_failure_returns_empty_and_warns(monkeypatch, caplog) -> None:
    """⚠️ 读失败 ⇒ 空 dict **且留 warning**。

    空 dict 本身是诚实的（调用方补 ``"none"``），
    但**必须留痕** —— 静默返回空 ⇒ 排查的人分不出
    「DB 挂了」还是「真没信号」。
    """
    _patch(monkeypatch, [], exc=RuntimeError("db down"))
    with caplog.at_level("WARNING", logger="cpt.web.app"):
        out = web_app._latest_signal_statuses()  # type: ignore[attr-defined]  # noqa: SLF001
    assert out == {}
    assert any("signal statuses unavailable" in r.message for r in caplog.records), \
        "读失败没有留痕 —— 降级会变成静默"


def test_connection_is_always_closed(monkeypatch) -> None:
    """⚠️ 读失败时连接**也要关** —— 否则每次轮询漏一个连接。"""
    closed: list[bool] = []
    _patch(monkeypatch, [], exc=RuntimeError("db down"), closed=closed)
    web_app._latest_signal_statuses()  # type: ignore[attr-defined]  # noqa: SLF001
    assert closed, "异常路径下连接没关 ⇒ 泄漏"
