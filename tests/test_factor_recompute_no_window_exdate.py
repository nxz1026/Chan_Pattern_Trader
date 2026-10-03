"""「窗口内无除权」应写恒定因子，而不是整只拒写（R44）。

**全程离线**：不碰 DB、不联网。

## 为什么这条要单独测

实测（2026-10-03 真机）：切表后仍是占位的 177 只里，

    99 只  从��分红
    74 只  **全部**除权日早于本地 bar 起点 2024-01-02
     2 只  新上市，daily_bar 只有 3 根

那 74 只原来被「无任何可用除权台阶」整只拒写。除权日全在窗口之前，
意味着**窗口内一次除权都没有** ⇒ 因子恒定，而**恒定就是正确答案**。

拒写不只是「不够好」，它有实际代价：``failed`` 不进 ``done``，
于是每天的定时重算都会把这 74 只重新查一遍 —— 每天白烧 74 次东财调用，
且**永远失败**。

## 另一个容易踩的坑（这个测试也钉住了）

常数必须取 ``anchor``（库里最新一根的值），**不能**取 1.0：
``anchor_scale`` 在 steps 为空时按其 docstring 返回 1.0。占位票恰好也是 1.0，
所以测试里若只用占位票，**用错实现也能通过**。所以这里特意造一只
anchor=199.4 的票 —— 用错实现会让它的显示价格缩放 199 倍。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("fr_mod", ROOT / "scripts/factor_recompute.py")
fr = importlib.util.module_from_spec(_spec)
sys.modules["fr_mod"] = fr
_spec.loader.exec_module(fr)

from cpt.adapters.corporate_actions import CorporateAction  # noqa: E402


class _FakeConn:
    """够用即可：只回答 ``current_latest_factor`` 那条 SELECT。"""

    def __init__(self, anchor: float | None) -> None:
        self.anchor = anchor
        self.written: list[tuple] = []

    def cursor(self):  # noqa: ANN201
        return self

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a):  # noqa: ANN002, ANN204
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._sql = sql
        self._row = None if self.anchor is None else (self.anchor,)

    def fetchone(self):  # noqa: ANN201
        return self._row

    def commit(self) -> None:
        pass


class _FakeClient:
    """``process_code`` 收的是 **client**（它自己调 ``client._get_conn()``），
    不是裸 conn —— 踩过这个签名。"""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def _get_conn(self) -> _FakeConn:  # noqa: SLF001
        return self._conn


def _install(monkeypatch: pytest.MonkeyPatch, *,
             bars: list[tuple[int, float]], anchor: float | None,
             actions: list[CorporateAction], written: list[tuple]) -> Any:
    monkeypatch.setattr(fr, "load_recent_closes", lambda conn, code, limit=800: tuple(bars))
    monkeypatch.setattr(fr, "current_latest_factor", lambda conn, code: anchor)
    monkeypatch.setattr(
        fr, "save_recompute_factors", lambda conn, rows: written.extend(rows) or len(rows)
    )

    class _Src:
        name = "eastmoney"
        fatal_errors = ()
        transient_errors = ()

        def fetch_corporate_actions(self, code):  # noqa: ANN001, ANN201
            return tuple(actions)

    return _Src()


def _client(anchor: float | None) -> _FakeClient:
    return _FakeClient(_FakeConn(anchor))


def _bars(n: int = 60) -> list[tuple[int, float]]:
    """60 根 bar，起始 2024-01-02（毫秒），逐日。"""
    import datetime as dt

    d0 = dt.date(2024, 1, 2)
    base = int(dt.datetime(2024, 1, 2, tzinfo=dt.UTC).timestamp() * 1000)
    return [(base + i * 86_400_000, 10.0 + i) for i in range(n)]


def test_all_ex_dates_before_window_writes_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心回归：除权日全早于窗口 ⇒ 写恒定，**不是**拒写。"""
    written: list[tuple] = []
    acts = [CorporateAction(ex_date="2022-06-16", cash_pre_tax=0.3, share_bonus=None,
                            transfer=None, source="eastmoney", status="实施分配")]
    src = _install(monkeypatch, bars=_bars(), anchor=1.0, actions=acts, written=written)

    res, _ = fr.process_code(_client(1.0), src, "000761", write=True, retries=0)

    assert res.ok, f"应判为成功，却拒写了：{res.note!r}"
    assert written, "一行都没写"
    assert len({round(r[2], 12) for r in written}) == 1, "因子应当恒定"
    assert "无除权" in written[0][4], f"basis 应说明原因：{written[0][4]!r}"


def test_constant_uses_anchor_not_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 关键：常数取 **anchor**，不是 1.0。

    占位票的 anchor 恰好也是 1.0，所以只用占位票当样本的话，
    **用错实现这个测试也会绿**。这里特意用 anchor=199.4 的票。
    """
    written: list[tuple] = []
    acts = [CorporateAction(ex_date="2022-06-16", cash_pre_tax=0.3, share_bonus=None,
                            transfer=None, source="eastmoney", status="实施分配")]
    src = _install(monkeypatch, bars=_bars(), anchor=199.4, actions=acts, written=written)

    fr.process_code(_client(199.4), src, "000001", write=True, retries=0)

    assert written
    vals = {round(r[2], 9) for r in written}
    assert vals == {199.4}, (
        f"常数应等于 anchor 199.4（否则显示价格会缩放 199 倍），实得 {vals}"
    )


def test_still_refuses_when_ex_date_inside_window_has_no_prev_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**不能**放宽过头：除权日落在窗口内、却取不到「除权前收盘」⇒ 仍要拒写。

    那种情况跳过一个台阶会让整段因子系统性偏小（handoff 明确说这是正确行为）。

    ⚠️ 边界选的是「除权日 == 第一根 bar 的日期」：它是**唯一**既在窗口内
    (``d < first_bar`` 为假、走主路径) 又没有更早 bar 的情形。
    换成窗口中间的日期(2024-03-01) 是**测不到的** —— 那时前收存在、
    算出台阶是正确行为，测试会误判成"放宽过头"。
    """
    written: list[tuple] = []
    acts = [CorporateAction(ex_date="2024-01-02", cash_pre_tax=0.3, share_bonus=None,
                            transfer=None, source="eastmoney", status="实施分配")]
    src = _install(monkeypatch, bars=_bars(), anchor=1.0, actions=acts, written=written)

    res, _ = fr.process_code(_client(1.0), src, "688089", write=True, retries=0)

    assert not res.ok, "除权日无前收时不该判成功"
    assert not written, "拒写时不该有任何行落库"


def test_no_actions_at_all_still_reports_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """完全没有公司行动 ⇒ 保持原有文案（这是「查无此记录」，不是「窗口内无除权」）。"""
    written: list[tuple] = []
    src = _install(monkeypatch, bars=_bars(), anchor=1.0, actions=[], written=written)

    res, _ = fr.process_code(_client(1.0), src, "001239", write=True, retries=0)

    assert not res.ok
    assert "无公司行动记录" in res.note
    assert not written


def test_too_few_bars_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """新股（K 线不足 30 根）仍然拒写 —— 不受本次改动影响。"""
    written: list[tuple] = []
    acts = [CorporateAction(ex_date="2024-03-01", cash_pre_tax=0.3, share_bonus=None,
                            transfer=None, source="eastmoney", status="实施分配")]
    src = _install(monkeypatch, bars=_bars(10), anchor=1.0, actions=acts, written=written)

    res, _ = fr.process_code(_client(1.0), src, "688089", write=True, retries=0)

    assert not res.ok
    assert "K 线不足" in res.note
