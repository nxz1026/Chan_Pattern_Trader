"""`get_queue` 必须接收**后来补传**的 ``on_status``（R45）。

**离线**：假 provider，不发任何 HTTP。

## 这条 bug 的触发条件在生产上**必然成立**

`cpt/application/llm_cases.py` 有**两个** ``get_queue`` 调用点：

    _bootstrap()  →  get_queue(on_status=on_llm_status)   ← 真正落库的那一方
    list_calls()  →  get_queue()                          ← 不传回调

而 ``list_calls`` 是 ``GET /api/dashboard/llm/calls`` 的处理函数 ——
**看板打开就会轮询它**（R45 真机 CDP 抓包：首屏即调 ``/llm/calls?limit=20``）。

原实现里 ``get_queue`` 见到 ``_QUEUE is not None`` 就直接 ``return``，
把新回调**静默丢掉**。于是：

    用户先打开过看板 → 队列带着空回调建好
    再点「解释结构」 → on_status 被丢弃、永远不注册
    ⇒ LLM 跑完了，cpt_llm_call 里那条**永远停在 queued**，UI 永远转圈

复现实测：修复前审计回调被调用 **0 次**，修复后 **2 次**。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from cpt.llm import get_queue, reset_queue
from cpt.llm.base import LLMRequest, LLMResult
from cpt.llm.config import LLMConfig
from cpt.llm.queue import Job

_CFG = LLMConfig(
    enabled=True, provider="openai_compatible", base_url="http://x", model="m", api_key="k"
)


class _FakeClient:
    def complete(self, request: LLMRequest) -> LLMResult:
        return LLMResult(text="ok", model="m")


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import cpt.llm.registry as reg

    monkeypatch.setattr(reg, "build_client", lambda cfg: _FakeClient())
    reset_queue()
    yield
    reset_queue()


def test_callback_registered_after_queue_exists() -> None:
    """核心回归：先 ``get_queue()``，再 ``get_queue(on_status=cb)`` —— 回调必须生效。"""
    seen: list[tuple] = []

    def cb(call_id: str, status: str, detail: str, result: Any = None) -> None:
        seen.append((call_id, status))

    first = get_queue(config=_CFG)  # 模拟看板轮询先到
    assert first is not None
    second = get_queue(config=_CFG, on_status=cb)  # 模拟 _bootstrap 后到
    assert second is first, "应当是同一个单例"

    second.submit(Job(request=LLMRequest(purpose="t", system="s", user="u"), call_id="probe-1"))
    second.drain(timeout=5.0)

    assert seen, "审计回调一次都没被调用 —— on_status 被静默丢弃了"
    assert any(status == "running" for _, status in seen)


def test_later_callback_overrides_earlier() -> None:
    """后设的回调应**覆盖**先设的（``_bootstrap`` 才是真正的落库方）。"""
    first_seen: list[str] = []
    second_seen: list[str] = []

    q = get_queue(config=_CFG, on_status=lambda i, s, d, r=None: first_seen.append(s))
    assert q is not None
    get_queue(config=_CFG, on_status=lambda i, s, d, r=None: second_seen.append(s))

    q.submit(Job(request=LLMRequest(purpose="t", system="s", user="u"), call_id="p2"))
    q.drain(timeout=5.0)

    assert not first_seen, "旧回调不该再被调用"
    assert second_seen, "新回调应接管"


def test_existing_queue_survives_config_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """队列已存在时，**不能**因为环境变量被改成停用就返回 ``None``。

    这条盯的是 R45 修法里的**顺序**：补注册那段必须早于 ``load_config()``。
    先读配置的话，一个已经正常运行的队列会被凭空藏起来 ——
    队列还在跑、线程还在打 LLM，但调用方拿到 ``None``。
    """
    import cpt.llm as pkg

    q = get_queue(config=_CFG)
    assert q is not None

    # 假装环境被改成停用
    monkeypatch.setattr(pkg, "load_config", lambda *a, **k: LLMConfig(enabled=False))

    again = get_queue()  # 不传 config ⇒ 会走 load_config
    assert again is q, "已有队列不该因为配置变成停用就被藏起来"


def test_set_on_status_is_thread_safe() -> None:
    """worker 线程读、HTTP 线程写 ⇒ 换回调不能读到「半截」状态。"""
    q = get_queue(config=_CFG)
    assert q is not None
    stop = threading.Event()
    errors: list[BaseException] = []

    def flipper() -> None:
        try:
            while not stop.is_set():
                q.set_on_status(lambda i, s, d, r=None: None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=flipper, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for _ in range(200):
            q.submit(Job(request=LLMRequest(purpose="t", system="s", user="u"), call_id="race"))
        q.drain(timeout=5.0)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)

    assert not errors, f"并发换回调炸了：{errors[:2]}"
