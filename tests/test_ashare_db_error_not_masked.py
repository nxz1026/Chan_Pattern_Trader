"""A 股快照：DB 故障不得被报成「缺因子」（R45）。

**离线**：全 fake client，不连 DB。

## 这个问题为什么值得单独立测试

本文件在别处（第 202 行附近）**白纸黑字**写着：

    必须与「没数据」和「DB 挂了」分开报 —— 报成 db_error 会把排查方向带偏（实测踩过）

而 `_try_on_demand_factors` 里却写着：

    except Exception:  # noqa: BLE001 — DB 类问题不该在这里吞掉，交给外层
        return None          # ← return 就是吞掉，与注释相反

三处后果：

1. 不记日志 ⇒ DB 抖动完全不可见；
2. 返回 ``None`` ⇒ 调用方 ``_reason_for_failure(None)`` 报成 **``no_factor``** ——
   **把 DB 故障说成缺因子**，正是本文件警告过的事；
3. **不 rollback** ⇒ 连接留在 aborted 态。``owns_client=False`` 时是**共享连接**，
   一条 SQL 失败会连锁毒掉后面所有查询（``_rollback_quietly`` 的 docstring 记着
   实测症状：「一只查失败，后面几十只全部降级/skip」）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.adapters.a_share_local import (  # noqa: E402
    AShareLocalError,
    AShareNoDataError,
    AShareNoFactorError,
)
from cpt.application import a_share_snapshot as snap  # noqa: E402


class _BoomConn:
    """execute 失败 ⇒ 连接进 aborted 态。"""

    def __init__(self) -> None:
        self.aborted = False
        self.rollbacks = 0

    def cursor(self):  # noqa: ANN201
        return self

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, *a: Any, **k: Any) -> None:
        self.aborted = True
        raise AShareLocalError("simulated DB failure")

    @property
    def rowcount(self) -> int:
        return 0

    def fetchone(self) -> Any:
        return None

    def fetchall(self) -> Any:
        return ()

    def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Client:
    """``fetch_validated_klines`` 抛 DB 错误的假客户端。"""

    def __init__(self) -> None:
        self.conn = _BoomConn()

    def _get_conn(self) -> Any:  # noqa: ANN202
        return self.conn

    def fetch_validated_klines(self, *a: Any, **k: Any) -> Any:
        raise AShareLocalError("simulated DB failure")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 核心：_try_on_demand_factors 不得吞掉 DB 错误
# ---------------------------------------------------------------------------


def test_db_error_is_not_swallowed_by_on_demand_fetcher() -> None:
    """DB 错误必须**冒出去**，让外层如实报 ``db_error``。"""
    client = _Client()
    with pytest.raises(AShareLocalError):
        snap._try_on_demand_factors(  # type: ignore[arg-type]
            "600519", client, 0, 1, ensurer=object()
        )


def test_no_data_still_returns_none() -> None:
    """对照组：**真的**没行情 → 仍应返回 ``None``（不是故障）。"""

    class _NoData(_Client):
        def fetch_validated_klines(self, *a: Any, **k: Any) -> Any:
            raise AShareNoDataError("no bars")

    out = snap._try_on_demand_factors(  # type: ignore[arg-type]
        "600519", _NoData(), 0, 1, ensurer=object()
    )
    assert out is None


def test_no_ensurer_returns_none_without_calling_client() -> None:
    """没配置 ensurer → 直接 ``None``，不碰客户端。"""
    client = _Client()
    assert snap._try_on_demand_factors("600519", client, 0, 1, ensurer=None) is None
    assert client.conn.rollbacks == 0


# ---------------------------------------------------------------------------
# 外层：DB 故障必须 rollback（共享连接时尤其重要）
# ---------------------------------------------------------------------------


def test_outer_handler_rolls_back_shared_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """``owns_client=False``（共享连接）时，DB 失败**必须**回滚。

    不回滚的后果：这条连接被留在 aborted 态，后面所有用它查的代码全部报
    ``InFailedSqlTransaction`` —— 一只票查失败，连累后面几十只。
    """
    client = _Client()
    monkeypatch.setattr(snap, "AShareLocalClient", lambda *a, **k: client)
    # 强制走「借用连接」分支
    monkeypatch.setattr(snap, "factor_ensurer_from_env", lambda *a, **k: None)

    out = snap.build_ashare_snapshot("600519", client=client)  # type: ignore[arg-type]
    # reason 有两个落点（empty_ashare_snapshot 同时写 data_quality 与 runtime）
    reason = out["data_quality"]["reason"]
    assert out["data_quality"]["degraded"] is True, "DB 故障必须标 degraded"
    assert out["runtime"]["status"] == "empty"
    assert out["runtime"]["degraded_reason"] == reason
    assert reason.startswith("db_error"), (
        f"DB 故障被报成 {reason!r} —— 会把排查方向带偏到『缺因子』"
    )
    assert client.conn.rollbacks >= 1, "共享连接出错后没有 rollback，连接留在 aborted 态"
    assert client.conn.aborted is False


def test_reason_for_failure_never_labels_db_error_as_no_factor() -> None:
    """``_reason_for_failure(None)`` 是「没配置」而非「DB 挂了」，调用方要能区分。"""
    assert snap._reason_for_failure(None) == "no_factor"
    # 关键：DB 错误**不能**走到这里 —— 见上面两个测试
