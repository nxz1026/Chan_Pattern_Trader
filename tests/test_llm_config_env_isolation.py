"""``load_config(environ)`` 不得改写进程环境（R45）。

**全程离线**：不联网、不需要真实 key。

## 这一条为什么值得单独立测试

`cpt/llm/config.py` 是**整个 LLM 层唯一读密钥的地方**（``CPT_LLM_API_KEY``）。
它原来的「传 dict 就只读这份 dict」实现方式是::

    previous = dict(os.environ)
    os.environ.clear()            # ← 清空整个进程环境
    os.environ.update(environ)
    try:    return load_config()
    finally: os.environ.clear(); os.environ.update(previous)

注释写的是「不碰进程全局」，**而它字面上就在改进程全局**。

CPT 是多线程的（``ThreadingHTTPServer`` + LLM worker 线程）。任何线程在
``clear()`` 与 ``update()`` 之间读环境变量，拿到的都是**残缺甚至空**的环境 ——
包括 ``RDSHOST`` / ``DB_PW`` / 飞书 webhook。

今天只有 ``tests/test_llm_layer.py`` 传参，但 ``load_config`` 是**公开函数**，
谁哪天在生产路径上用了它，整台服务的环境会在那一瞬间消失。
"""

from __future__ import annotations

import os
import threading

import pytest
from cpt.llm.config import LLMConfig, load_config

_SANE_ENV = {
    "RDSHOST": "db.internal",
    "DB_PW": "super-secret-db-password",
    "CPT_FEISHU_WEBHOOK": "https://hooks.example/secret-token",
}


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """给 os.environ 装一份含 DB 凭据的基线，测试后自动还原。"""
    for key, value in _SANE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CPT_LLM_ENABLED", "0")


def test_loading_with_environ_does_not_touch_process_env(isolated_env: None) -> None:
    """核心回归：传 dict 之后，``os.environ`` 必须**原样**。

    这条在旧实现下会失败：``clear()`` 会把 RDSHOST/DB_PW/飞书 webhook 全抹掉。
    """
    before = dict(os.environ)
    load_config({"CPT_LLM_ENABLED": "1", "CPT_LLM_MODEL": "test-model"})
    after = dict(os.environ)

    missing = set(before) - set(after)
    assert not missing, f"os.environ 少了键：{sorted(missing)[:5]}"
    assert after == before, "os.environ 被改动了"


def test_db_credentials_survive_a_config_load(isolated_env: None) -> None:
    """DB 凭据必须**毫发无损** —— 它与 LLM 配置毫无关系。"""
    load_config({"CPT_LLM_ENABLED": "1", "CPT_LLM_API_KEY": "sk-llm-key"})
    assert os.environ["DB_PW"] == _SANE_ENV["DB_PW"]
    assert os.environ["RDSHOST"] == _SANE_ENV["RDSHOST"]
    assert os.environ["CPT_FEISHU_WEBHOOK"] == _SANE_ENV["CPT_FEISHU_WEBHOOK"]


def test_concurrent_readers_never_see_an_empty_environment() -> None:
    """并发读者不能看到被清空的环境。

    旧实现在 ``clear()`` 与 ``update()`` 之间有一个**真实的窗口**，
    期间任何读环境变量的线程都会拿到空字典。这条用真实线程复现它。
    """
    os.environ["DB_PW"] = _SANE_ENV["DB_PW"]
    seen: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            seen.append(dict(os.environ))
            pass  # 让出 GIL，制造交错

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            load_config({"CPT_LLM_ENABLED": "1"})
    finally:
        stop.set()
        thread.join(timeout=2)

    assert seen, "读者线程没跑到 —— 测试本身失效"
    empty = [s for s in seen if not s]
    assert not empty, f"有 {len(empty)} 次读到了**空环境**（旧实现会这样）"
    no_db = [s for s in seen if "DB_PW" not in s]
    assert not no_db, f"有 {len(no_db)} 次读不到 DB_PW"


def test_environ_arg_still_works_as_override(isolated_env: None) -> None:
    """修的是实现方式，不是功能：传 dict 仍应**优先于** os.environ。"""
    cfg = load_config(
        {
            "CPT_LLM_ENABLED": "1",
            "CPT_LLM_PROVIDER": "openai_compatible",
            "CPT_LLM_BASE_URL": "https://example.invalid/v1/chat/completions",
            "CPT_LLM_MODEL": "fake-model",
            "CPT_LLM_API_KEY": "sk-fake",
            "CPT_LLM_MAX_ATTEMPTS": "9",
        }
    )
    assert cfg.enabled is True
    assert cfg.model == "fake-model"
    assert cfg.api_key == "sk-fake"
    assert cfg.max_attempts == 9


def test_no_arg_still_reads_process_env(isolated_env: None) -> None:
    """无参调用照旧读 ``os.environ``（生产路径）。"""
    os.environ["CPT_LLM_MODEL"] = "from-process"
    os.environ["CPT_LLM_ENABLED"] = "1"
    cfg = load_config()
    assert cfg.model == "from-process"
    assert cfg.enabled is True


def test_api_key_never_appears_in_repr_or_redacted() -> None:
    """密钥不许出现在 repr / redacted() 里 —— 那是它最容易被带出去的两条路。"""
    cfg = LLMConfig(
        enabled=True,
        provider="openai_compatible",
        base_url="u",
        model="m",
        api_key="sk-super-secret",
    )
    assert "sk-super-secret" not in repr(cfg), "repr() 泄露了 api_key"
    red = cfg.redacted()
    assert "sk-super-secret" not in str(red), "redacted() 泄露了 api_key"
    assert red["has_api_key"] is True
