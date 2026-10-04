"""外部/装饰性查询的**失败降级**（R45 P0-1 补测 · 第二批：外部接口）。

## 补什么

两个「查询失败时必须**降级而不是崩**」的函数，它们都在**每张 A 股快照**上跑：

- ``a_share_routes._names``（45.5%）—— 查证券名。**R45 新加的
  ``submit_llm_summarize`` 每次都调它**。
- ``a_share_factor.hot_pool_codes``（29.2%）—— 热门池裸码。

## 为什么它们的失败处理值得钉

两者都是**装饰性数据**：名字、热门池。查不到时正确行为是
「少显示一点」，**不是**把整张快照带崩。

⚠️ 关键区别在于降级的**可观察性**：
``_names`` 用 ``_LOG.debug``（名字丢了不该吵），
而若哪个降级被改成静默 ``return``，用户会看到「票没有名字」
却**完全不知道为什么** —— 与今天修的 storage「失败与空同码」同一类。

全程离线：DB 与外部查询都换假对象。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.adapters import a_share_factor  # noqa: E402
from cpt.web import a_share_routes  # noqa: E402


class _BoomCur:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.queries: list[str] = []

    def execute(self, sql: str, *a: object) -> None:
        self.queries.append(" ".join(sql.split())[:70])
        raise self._exc

    def __enter__(self) -> "_BoomCur":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def fetchall(self) -> list:
        return []

    def fetchone(self):  # noqa: ANN201
        return None


class _BoomConn:
    def __init__(self, exc: Exception) -> None:
        self._cur = _BoomCur(exc)

    def cursor(self) -> _BoomCur:
        return self._cur


# ── _names ────────────────────────────────────────────────────
def test_names_failure_degrades_to_empty_dict(monkeypatch) -> None:
    """查名失败 ⇒ **返回空字典**，不抛。

    名字是装饰：查不到就不显示名字，**不能把整张快照带崩**。
    """
    import cpt.adapters.a_share_local as local

    def _boom(_codes: list[str]):
        raise RuntimeError("db down")

    monkeypatch.setattr(local, "fetch_security_names", _boom)
    assert a_share_routes._names(["600519"]) == {}  # noqa: SLF001


def test_names_failure_is_debug_not_error(monkeypatch, caplog) -> None:
    """降级必须**留痕** —— 哪怕只是 debug。

    ⚠️ 如果哪���天把它改成静默 ``return {}``，用户只会看到「票没名字」
    而完全不知道为什么 —— 与 storage「失败与空同码」是同一类问题。
    """
    import cpt.adapters.a_share_local as local

    monkeypatch.setattr(
        local, "fetch_security_names",
        lambda _c: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("DEBUG", logger="cpt.web.a_share_routes"):
        a_share_routes._names(["600519"])  # noqa: SLF001
    assert any("证券名称" in r.message for r in caplog.records), \
        "失败没有留痕 —— 降级会变成静默"


def test_names_success_passes_through(monkeypatch) -> None:
    """对照组：成功时**原样返回**，降级逻辑不能吞掉正常结果。"""
    import cpt.adapters.a_share_local as local

    monkeypatch.setattr(local, "fetch_security_names", lambda c: {c[0]: "贵州茅台"})
    assert a_share_routes._names(["600519"]) == {"600519": "贵州茅台"}  # noqa: SLF001


# ── hot_pool_codes ────────────────────────────────────────────
# ⚠️ **第一版我按「查不到要降级为空」写测试，那是我的假设，不是它的契约。**
# 读完实现才发现：它**没有**任何异常处理，DB 错误直接往上抛。
# 而这是**对的** —— 它只被 `factor_recompute` / `golden_set` 这两个**脚本**调用，
# 脚本需要知道自己的处理范围；DB 挂了却静默返回空池 = 只处理 0 只票却报「成功」，
# 那比崩掉糟得多。
# ⇒ 这里钉的是「**去重 + 剥后缀 + 限量 + 出错不静默**」。
class _Cur:
    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.step = 0

    def execute(self, sql: str, *a: object) -> None:
        if "max(" in sql:
            self._rows.append([None])          # 没有历史数据 ⇒ 跳过该源

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *e: object) -> None:
        return None

    def fetchone(self):  # noqa: ANN201
        return self._rows[-1] if self._rows else None

    def fetchall(self) -> list:
        return []


class _Conn:
    def __init__(self, rows: list | None = None) -> None:
        self._rows = rows or []

    def cursor(self) -> _Cur:
        return _Cur(self._rows)


def test_hot_pool_empty_sources_yields_empty_tuple() -> None:
    """两个源都没有数据 ⇒ 空元组（**不是** None，也不是抛）。"""
    assert a_share_factor.hot_pool_codes(_Conn()) == ()  # type: ignore[arg-type]


def test_hot_pool_propagates_db_error_fails_loud() -> None:
    """⚠️ DB 错误**必须往上抛**，不得静默返回空池。

    只被脚本调用，而脚本要靠它决定处理范围 ——
    静默返回空池会让「一次都没处理」看起来像「跑完了」。
    """
    with pytest.raises(RuntimeError):
        a_share_factor.hot_pool_codes(_BoomConn(RuntimeError("db down")))  # type: ignore[arg-type]


def test_hot_pool_respects_limit() -> None:
    """限量必须生效（重算要按额度分几天）。"""
    class _ManyCur:
        def __init__(self) -> None:
            self._date: object = None

        def execute(self, sql: str, *a: object) -> None:
            if "max(" in sql:          # 先问最新日期 ⇒ 后面才会去取该日明细
                self._date = "2026-10-04"

        def __enter__(self) -> "_ManyCur":
            return self

        def __exit__(self, *e: object) -> None:
            return None

        def fetchone(self):  # noqa: ANN201
            return [self._date]

        def fetchall(self) -> list:
            return [[f"6005{i:02d}"] for i in range(100)]

    class _Conn2:
        def cursor(self) -> _ManyCur:
            return _ManyCur()

    out = a_share_factor.hot_pool_codes(_Conn2(), limit=5)  # type: ignore[arg-type]
    assert len(out) == 5, f"限量失效：拿到 {len(out)} 个"


def test_hot_pool_dedupes_and_strips_suffix() -> None:
    """``600519.SH`` 剥成 ``600519``，且**跨两个源去重**。"""
    class _Cur2:
        def __init__(self) -> None:
            self._n = 0

        def execute(self, sql: str, *a: object) -> None:
            pass

        def __enter__(self): return self
        def __exit__(self, *e: object): return None
        def fetchone(self):  # noqa: ANN201
            self._n += 1
            return ["2026-10-04"]
        def fetchall(self) -> list:
            return [["600519.SH"]] if self._n % 2 else [["600519.SZ"]]

    class _Conn3:
        def cursor(self): return _Cur2()

    out = a_share_factor.hot_pool_codes(_Conn3())  # type: ignore[arg-type]
    assert out == ("600519",), out
