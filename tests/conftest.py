"""全局测试夹具。

## 为什么必须有一条"按需补因子"的兜底

R17-3 的按需补因子会在本地因子缺失时**联网拉腾讯并写生产库**。默认关闭（见
``cpt.application.a_share_snapshot.factor_ensurer_from_env``），但**生产入口
``cpt.web.a_share_routes.snapshot_payload`` 是显式打开的** —— 于是任何打到
``/api/dashboard/a-share/snapshot`` 的测试都会走真实链路。

这是踩出来的：早期版本把默认写成"开"，跑一次 ``pytest`` 就让
``test_snapshot_reason_no_factor_is_not_db_error`` 真的去腾讯拉了 600519 的 800 行
因子并写进生产库（因子表 94 → 95 只）。所以这里**无条件**把它关掉，需要测试该
路径的用例自己用 ``monkeypatch.setenv(..., "1")`` 显式打开并注入假取数函数。

## ``served()``：起真 server 的共用夹具（F3-④）

``threading.Thread(target=server.serve_forever, daemon=True)`` 这段样板原先在
``tests/`` 里复制了 **8 处**（7 个文件），每份都要自己写 ``shutdown`` /
``server_close`` / ``thread.join(timeout=2)`` 三连，漏一处就泄漏线程和端口。
统一收进 ``served(provider)``：进入时 yield base URL，退出时保证三连回收。

**不改各测试的既有假设**：仍然绑 ``127.0.0.1``、仍然 ``port=0``（由内核分配，
避免固定端口冲突）、``join`` 超时仍是 2s。
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

#: 与 ``cpt.application.a_share_snapshot.ENV_ONDEMAND_FACTOR`` 同值。
#: 这里写字符串字面量而不是 import：conftest 不该把被测包拖进 collection 阶段
#: （CI 模拟里 cpt 的依赖可能被屏蔽）。
_ONDEMAND_ENV = "CPT_ASHARE_ONDEMAND_FACTOR"

#: ``served()`` 未显式给 provider 时的最小可用 v2 快照。
#: 与各路由测试原先各自内联的那份**逐字相同**（只有 ``schema_version`` + 空 ``candles``）。
_MINIMAL_SNAPSHOT: dict[str, Any] = {"schema_version": "dashboard.v2", "candles": []}

#: 无头 Chrome 的公共参数。
#:
#: ``--disable-dev-shm-usage`` 是关键：GitHub runner 的 ``/dev/shm`` 默认只有 64MB，
#: 不关掉共享内存文件时 Chrome 会**随机**卡死到超时 —— 2026-09-25 排查时 CI 三次
#: 运行里两次 ``subprocess.TimeoutExpired``，3.12 / 3.14 两条腿都中过，与 Python
#: 版本无关（一开始误以为是 3.14 专属问题）。
CHROME_FLAGS: tuple[str, ...] = (
    "--headless",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-extensions",
    "--disable-sync",
)

#: 浏览器子进程超时（秒）。CI 共享 runner 上 Chrome 冷启动偶尔很慢，30s 会假红。
CHROME_TIMEOUT_SECONDS = 60


def chromium_path() -> str | None:
    """找一个可用的 Chromium/Chrome；**找不到就返回 None**，让 ``skipif`` 生效。

    这里必须逐个 ``Path(...).exists()`` 校验。早先
    ``test_dashboard_chromium_interactions.py`` 写的是
    ``shutil.which("chromium") or str(Path.home() / ".local/bin/chromium")`` ——
    后半段**不校验存在性**，于是"本机没装 chromium"时它也不是 ``None``，
    ``skipif`` 形同虚设、测试直接 ``AssertionError``（本机假红、CI 假红）。
    两个文件各写一份实现正是它漂移的原因，故收到这里共用。
    """
    candidates = (
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        Path.home() / ".local/bin/chromium",
        Path.home() / ".cache/ms-playwright/chromium-1243/chrome-linux-arm64/chrome",
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


@pytest.fixture(autouse=True)
def _disable_ondemand_factor_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """整个测试会话禁止按需补因子：不联网、不写库。"""
    monkeypatch.setenv(_ONDEMAND_ENV, "0")


@contextmanager
def served(provider: object = None) -> Iterator[str]:
    """起一个真 HTTP server，yield 它的 base URL；退出时关停并回收线程。

    ``provider`` 可以是 ``SnapshotProvider``（零参 callable）或任何带
    ``snapshot_payload()`` 的对象 —— 即 ``cpt.web.app.serve_snapshot`` 接受的两种形态；
    传 ``None`` 时用最小 v2 快照。

    ``cpt`` 在函数体内**延迟导入**，与上面 ``_ONDEMAND_ENV`` 同理：conftest 在
    collection 阶段不碰被测包。

    用法::

        from tests.conftest import served

        with served(lambda: {"schema_version": "dashboard.v2"}) as base:
            urllib.request.urlopen(f"{base}/api/dashboard/snapshot")
    """
    from cpt.web.app import serve_snapshot

    if provider is None:
        provider = lambda: dict(_MINIMAL_SNAPSHOT)  # noqa: E731

    server = serve_snapshot(provider)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
