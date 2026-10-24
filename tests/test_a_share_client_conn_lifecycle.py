"""AShareLocalClient 的**连接生命周期**（2026-10-24）。

背景：它懒连接后从不关闭，而调用方每次都 new 一个 ⇒ **每个请求泄漏一条连接**
⇒ 跑久了打满 ``max_connections`` ⇒ **同机所有依赖 DB 的服务一起挂**
（线上实测 ``FATAL: sorry, too many clients already``）。

⇒ 这里钉住「client 被回收 ⇒ 连接被关」。
"""

from __future__ import annotations

import gc

from cpt.adapters.a_share_local import AShareLocalClient


class _FakeConn:
    """能记账的假连接。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_close_is_idempotent_and_releases_conn() -> None:
    """close() 关掉连接，且可重复调用。"""
    client = AShareLocalClient()
    conn = _FakeConn()
    client._conn = conn
    client.close()
    assert conn.closed is True
    client.close()  # 幂等 ⇒ 不该抛
    assert conn.closed is True


def test_client_closes_its_connection_when_collected() -> None:
    """★ 回归守卫：client 出作用域被 GC ⇒ 连接必须被关。

    这就是线上那次「103 个 socket」的直接成因 ⇒ 不许再退化。
    """
    conn = _FakeConn()
    client = AShareLocalClient()
    client._conn = conn
    del client
    gc.collect()
    assert conn.closed is True, "client 被回收后连接必须关闭，否则会泄漏到打满 max_connections"


def test_context_manager_closes() -> None:
    """``with`` 用法也会关。"""
    conn = _FakeConn()
    client = AShareLocalClient()
    client._conn = conn
    with client:
        pass
    assert conn.closed is True
