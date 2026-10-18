"""P2-4 回归：运行环（``_RUN_RING`` / ``_RUN_BODIES``）在并发下必须原子。

背景：``cpt/web/app.py::_with_run_index`` 在 **32 线程**的 HTTP 服务器上**每请求**
调 :func:`record_run`。修复前两个 deque 无锁、读写与迭代全裸：

1. **错位配对**：两次交错 append 后 ``_RUN_RING[i]`` 与 ``_RUN_BODIES[i]`` 属于
   不同 run，:func:`run_body` 返回**另一个 run 的本体** ⇒ ``/compare`` 与
   ``/multi-run`` 拿到错误基线。
2. ``RuntimeError: deque mutated during iteration``：append 撞上 :func:`run_body`
   的 ``zip(reversed(_RUN_RING), reversed(_RUN_BODIES))`` 迭代（子代理 8 试 7 中），
   经 P1-1（``do_GET`` 无兜底）变成连接重置。

**为什么用故障注入而不是裸并发**：裸的 32 线程 barrier 并发在 CPython 上命不中
那两行之间的调度窗口（实测 20 次试验 0 次错位），这种测试会在锁被删掉后照样全绿
—— 等于没测。这里撑开真正的窗口（在第一次 ``ring.append`` 之后让出 GIL），
删掉锁会**确定性**变红。
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque

import pytest
from cpt.application import dashboard_runs as dr
from cpt.application.dashboard_runs import (
    RUN_RING_SIZE,
    clear_runs,
    recent_runs,
    record_run,
    run_body,
)


@pytest.fixture(autouse=True)
def _clean_ring():
    clear_runs()
    yield
    clear_runs()


def _snapshot(tag: str) -> dict:
    """一份带**本体身份**的快照：``candles`` 里写着它属于哪个 run。"""
    return {
        "market": {"symbol": "BTCUSDT", "interval_ms": 300_000, "bar_count": 1},
        "candles": [{"tag": tag}],
        "runtime": {"data_source": "fixture", "status": "confirmed", "run_id": tag},
        "reproducibility": {"dataset_hash": tag, "config_hash": "cfg", "generated_at": 1},
    }


def _record_four_concurrently() -> tuple[list[str], list[str | None]]:
    """4 个线程同时记账，绕开 ``record_run`` 内部的 ``_RUN_RING`` 名字绑定。

    锁保护的是 ``record_run`` 里「查重 + 两次 append」这一段；只要持锁，
    ``_RUN_RING`` 与 ``_RUN_BODIES`` 的 append 之间不可能插入别的记账线程。
    """
    tags = ["A", "B", "C", "D"]
    barrier = threading.Barrier(len(tags))

    def worker(tag: str) -> None:
        barrier.wait(timeout=5)
        record_run(_snapshot(tag))

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in tags]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return (
        [row["run_id"] for row in dr._RUN_RING],
        [body["candles"][0]["tag"] if body else None for body in dr._RUN_BODIES],
    )


class _FirstAppendYields(deque):
    """第一次 append 之后让出调度窗口的 deque。

    等价于「OS 恰在 ``_RUN_RING.append`` 与 ``_RUN_BODIES.append`` 之间抢走 GIL」——
    这正是无锁实现错位的窗口。用故障注入而不是靠运气，测试才可复现。
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._yielded = False

    def append(self, item: object) -> None:
        super().append(item)
        if not self._yielded:
            self._yielded = True
            time.sleep(0.05)


def test_index_and_body_stay_paired_when_gil_is_stolen_between_appends(monkeypatch) -> None:
    """⚠️ 核心：两次 append 之间被抢走 GIL 时，索引行与本体仍逐条对应。"""
    monkeypatch.setattr(dr, "_RUN_RING", _FirstAppendYields(maxlen=RUN_RING_SIZE))
    monkeypatch.setattr(dr, "_RUN_BODIES", deque(maxlen=RUN_RING_SIZE))

    tags, bodies = _record_four_concurrently()

    assert sorted(tags) == ["A", "B", "C", "D"], "四个 run 都该落进索引行"
    assert tags == bodies, "索引行与本体错位 —— record_run 的两次 append 不是原子的"


def test_run_body_returns_the_body_of_that_run(monkeypatch) -> None:
    """``run_body(run_id)`` 取到的本体必须**自带那个 run 的身份**，不是邻居的。"""
    monkeypatch.setattr(dr, "_RUN_RING", _FirstAppendYields(maxlen=RUN_RING_SIZE))
    monkeypatch.setattr(dr, "_RUN_BODIES", deque(maxlen=RUN_RING_SIZE))

    _record_four_concurrently()

    for row in recent_runs():
        body = run_body(row["run_id"])
        assert body is not None, f"{row['run_id']} 有索引行却没有本体"
        assert body["candles"][0]["tag"] == row["run_id"], (
            f"索引行 {row['run_id']} 配到了 {body['candles'][0]['tag']} 的本体"
        )


def test_concurrent_read_during_write_does_not_raise() -> None:
    """读写并发不得抛 ``RuntimeError: deque mutated during iteration``。

    修复前 ``run_body`` 用裸迭代器遍历两个 deque，写入线程一 append 就炸；
    该异常经 P1-1（``do_GET`` 无兜底）会变成连接重置。

    这里把线程切换间隔压到 5µs 把窗口撑开（实测无锁实现 6 次探针累计 9,168 例异常，
    有锁 0 例）；测完还原全局值。
    """
    stop = threading.Event()
    errors: list[BaseException] = []
    previous = sys.getswitchinterval()
    sys.setswitchinterval(5e-6)

    def writer() -> None:
        counter = 0
        try:
            while not stop.is_set():
                counter += 1
                record_run(_snapshot(f"w{counter}"))
        except BaseException as exc:  # noqa: BLE001 —— 线程里不能直接 raise
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                for row in recent_runs():
                    run_body(row["run_id"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        sys.setswitchinterval(previous)

    assert not errors, f"并发读写抛了异常：{errors[:3]}"


def test_recent_runs_returns_a_snapshot_not_a_live_view() -> None:
    """``recent_runs`` 必须在锁内定型 —— 返回活视图会被下一次 append 改变/打断。"""
    for index in range(5):
        record_run(_snapshot(f"r{index}"))
    rows = recent_runs()
    assert isinstance(rows, tuple)
    before = len(rows)
    record_run(_snapshot("r-extra"))
    assert len(rows) == before, "已返回的元组长度不该被后续记账改变"
    assert [row["run_id"] for row in rows] == [f"r{i}" for i in reversed(range(5))]


def test_ring_evicts_oldest_beyond_capacity_without_desync() -> None:
    """写满并越界驱逐后，索引行与本体仍逐条配对。"""
    for index in range(RUN_RING_SIZE + 7):
        record_run(_snapshot(f"e{index}"))
    rows = recent_runs()
    assert len(rows) == RUN_RING_SIZE, "环容量必须稳定在 RUN_RING_SIZE"
    for row in rows:
        body = run_body(row["run_id"])
        assert body is not None and body["candles"][0]["tag"] == row["run_id"]
