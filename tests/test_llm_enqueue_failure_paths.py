"""LLM 入队骨架的**失败分支**（R45 P0-1 覆盖率补测）。

## 为什么补这两个

R45 覆盖率扫描（`scripts/report_coverage_gaps.py`）把
``llm_cases._bootstrap`` 标成 **37.5%**（10/16 行未覆盖），
而它正是 R45 新增的「结构判断摘要」**建在上面的东西**。

两条未覆盖的分支都是**故障路径**，且都可能让用户看到错误结论：

1. ``mark_interrupted`` 写失败 ⇒ 被 ``except Exception`` 吞掉并降级为
   ``_LOG.info`` ⇒ 设计上就该不挡启动，但**没有测试证明它真的不挡**；
2. ``submit_recommendation`` 拿到 ``queue is None``（LLM 未启用 / 缺 key）
   ⇒ 必须回 ``available: False``，前端只把摘要行藏起来。
   这条路径**一次都没测过**，而它恰恰是最常见的情况（LLM 默认可能没开）。

⚠️ 本文件**全程离线**：不碰 DB、不联网、不真的调模型。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.application import llm_cases as lc  # noqa: E402


class _FakeConn:
    """``_write`` 会 ``conn.commit()``（llm_cases:79，app 层永远提交），
    所以假连接必须有这个方法 —— 第一版传了裸 ``object()`` 直接 AttributeError。"""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class _FakeClient:
    def __init__(self, conn: object) -> None:
        self._conn = conn
        self.closed = False

    def _get_conn(self) -> object:
        return self._conn

    def close(self) -> None:
        self.closed = True


def _patch_client(monkeypatch: pytest.MonkeyPatch, conn: object) -> list[bool]:
    """把 ``AShareLocalClient`` 换成假实现；返回记录 close 调用的列表。"""
    closed: list[bool] = []
    import cpt.adapters.a_share_local as mod

    class _C:
        def __init__(self) -> None:
            self._conn = conn

        def _get_conn(self) -> object:
            return self._conn

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(mod, "AShareLocalClient", _C)
    return closed


# ── _bootstrap ────────────────────────────────────────────────
def test_bootstrap_survives_interrupted_mark_failure(monkeypatch) -> None:
    """⚠️ 核心：表还没建 / DB 抖了，**不能挡住启动**。

    ``_bootstrap`` 的 ``except Exception`` 覆盖 ``mark_interrupted`` 失败，
    只记 info 日志就继续。降级本身是对的，但要有人证明它真的不炸。
    """
    from cpt.llm import base as llm_base

    calls: list[str] = []
    monkeypatch.setattr(lc, "mark_interrupted",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("表不存在")))
    monkeypatch.setattr(lc, "get_queue", lambda **k: (calls.append("queue"), "Q")[1])
    closed = _patch_client(monkeypatch, object())

    assert lc._bootstrap() == "Q"
    assert closed == [True], "连接没关 —— 泄漏"


def test_bootstrap_closes_conn_even_when_mark_raises(monkeypatch) -> None:
    """失败也要 ``close()`` —— 否则每次启动漏一个连接。"""
    monkeypatch.setattr(lc, "mark_interrupted",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(lc, "get_queue", lambda **k: "Q")
    closed = _patch_client(monkeypatch, object())
    lc._bootstrap()
    assert closed, "异常路径下连接没关"


# ── summarize / explain 的「LLM 不可用」分支 ────────────────────
@pytest.mark.parametrize(
    "fn", [lc.summarize_recommendation]
)
def test_summary_reports_unavailable_when_queue_missing(monkeypatch, fn) -> None:
    """queue 为 None（LLM 未启用 / 缺 key）⇒ available:False，**不抛**。

    这是**最常见**的情况（LLM 可能压根没开），却是覆盖率里唯一没测过的分支。
    前端拿到它会把摘要行藏起来、确定性结果照常显示 —— 这条契约必须钉住。
    """
    calls: list[str] = []
    monkeypatch.setattr(lc, "enqueue_call", lambda *a, **k: calls.append("enq") or True)
    monkeypatch.setattr(lc, "finish_call", lambda *a, **k: calls.append("finish"))
    monkeypatch.setattr(lc, "_bootstrap", lambda: None)          # ← 队列拿不到

    out = fn(
        _FakeConn(), code="600519", name="贵州茅台", action_label="观望",
        headline="信号已失效", reason="一买已失效", price=1258.62,
    )
    assert out["available"] is False
    assert out["reason"] == "llm_unavailable"
    assert "finish" in calls, "拿不到队列时必须把这条调用标成终态，否则永远卡在 queued"


@pytest.mark.parametrize("fn", [lc.summarize_recommendation])
def test_duplicate_returns_existing_call_id(monkeypatch, fn) -> None:
    """⚠️ R45 真 bug 的守卫：重复提交必须回**库里真实存在**的 call_id。

    ``enqueue_call`` 重复时返回 False，而 ``row["call_id"]`` 是**刚生成**的、
    根本没进库 ⇒ 前端拿着它轮询永远查不到 ⇒「明明算过，刷新就没了」。
    """
    # ⚠️ call_row 返回的是**普通 dict**（llm_call_store:129），
    # 我第一版按 dataclass 写、还 import 了一个不存在的 CallRow —— 全是猜的。
    monkeypatch.setattr(lc, "enqueue_call", lambda *a, **k: False)  # 重复
    # ⚠️ 要 patch **store 模块**里的那个 —— ``_existing_call_id`` 是
    # 函数内 `from cpt.storage.llm_call_store import recent_calls`，
    # patch ``llm_cases.recent_calls`` 对它**无效**（第一版踩了，
    # 于是真函数拿着假连接跑，报 no attribute 'cursor'）。
    import cpt.storage.llm_call_store as store

    monkeypatch.setattr(
        store, "recent_calls",
        lambda *a, **k: [{"call_id": "REAL-1", "request_hash": "DIGEST"}],
    )
    monkeypatch.setattr(lc, "request_hash", lambda *a: "DIGEST")
    monkeypatch.setattr(
        lc, "call_row",
        lambda **k: {"call_id": "FRESH-NOT-IN-DB", "request_hash": "DIGEST",
                     "status": "queued", "purpose": k.get("purpose", "")},
    )

    out = fn(
        _FakeConn(), code="600519", name="", action_label="观望",
        headline="h", reason="r", price=1.0,
    )
    assert out["status"] == "duplicate"
    assert out["call_id"] == "REAL-1", "回的是刚生成、库里不存在的 id"
    assert out["call_id"] != "FRESH-NOT-IN-DB"
