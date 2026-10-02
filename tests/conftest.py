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

## ``stub_realtime_quote``：按文件 opt-in 的东财外网 stub

``_attach_dual_compare`` 在**每次** ``build_ashare_snapshot`` 里都直连
``push2.eastmoney.com`` 并把**实时价**写进快照，所以「断言两次快照相等」的用例会
随机红（实测在 CI 上红过，已在修复前的 commit 上复现过同形状失败）。
``stub_realtime_quote`` 把这个响应钉成固定值。

与上面那条 autouse 不同，它**故意不做成 autouse** —— ``served()`` 起的真 server
要靠 ``urllib.request.urlopen`` 打 ``127.0.0.1``，全局替换会把那些真调用一起打死。
需要它的文件自己声明 ``pytestmark = pytest.mark.usefixtures("stub_realtime_quote")``。
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
#: ``--disable-dev-shm-usage`` 是**防御性**的（CI runner 的 ``/dev/shm`` 常见只有
#: 64MB）。但 2026-09-25 的 A/B 实验**没能证实它是 CI 超时的根因**：在私有 mount
#: namespace 里把 ``/dev/shm`` 压到 64MB 后，加 / 不加该参数各跑 8 次都是 **0 失败**。
#:
#: 站得住的解释是**争抢导致的变慢**：本机 4 核单跑 1.1s，12 路并发涨到 9.4–11.4s
#: （约 10×）。CI runner 核更少且与他人共享，30s 超时确实偏紧 ——
#: 所以真正起作用的处置是下面的 ``CHROME_TIMEOUT_SECONDS`` 提到 60，这些 flag 是
#: 顺带加固。**不要再把 flake 的根因写成 /dev/shm。**
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

#: 浏览器子进程超时（秒）。
#:
#: 30 → 60 是这次 flake 的**主要**处置：Chrome 在本机单跑只要 ~1.1s，但 4 核 12 路
#: 并发时涨到 ~11s（约 10×）。CI runner 核更少、还和别的 job 共享，冷启动叠加争抢
#: 后偶尔超过 30s 完全说得通（实测到的失败就是 ``subprocess.TimeoutExpired``）。
CHROME_TIMEOUT_SECONDS = 60


def chromium_path() -> str | None:
    """找一个可用的 Chromium/Chrome；**找不到就返回 None**，让 ``skipif`` 生效。

    这里必须逐个 ``Path(...).exists()`` 校验。早先
    ``test_dashboard_chromium_interactions.py`` 写的是
    ``shutil.which("chromium") or str(Path.home() / ".local/bin/chromium")`` ——
    后半段**不校验存在性**，于是"本机没装 chromium"时它也不是 ``None``，
    ``skipif`` 形同虚设、测试直接 ``AssertionError``（本机假红、CI 假红）。
    两个文件各写一份实现正是它漂移的原因，故收到这里共用。

    ## Windows 也认（2026-10-02 补）

    原先只枚举 Linux 路径，于是本机装了 Chrome 也返回 ``None``，
    ``test_dashboard_chromium_smoke.py`` 在 Windows 上**恒 skip**。

    skip 不会变 pass，所以它不掩盖任何失败 —— 但它让人以为「冒烟测试在 Windows
    上是绿的」，实际是根本没跑。这正是比红更糟的那种假象：绿灯来自没执行。
    """
    home = Path.home()
    candidates = (
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        # Windows：默认安装位 + winget 常见的 per-user 位
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(home / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        str(home / "AppData/Local/Microsoft/Edge/Application/msedge.exe"),
        # Linux / macOS
        str(home / ".local/bin/chromium"),
        str(home / ".cache/ms-playwright/chromium-1243/chrome-linux-arm64/chrome"),
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


@pytest.fixture(autouse=True)
def _disable_ondemand_factor_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """整个测试会话禁止按需补因子：不联网、不写库。"""
    monkeypatch.setenv(_ONDEMAND_ENV, "0")


#: 钉死的实时报价（新浪快照口径，**不复权**）：现价 100.00 元。
#: 字段口径见 ``cpt.adapters.a_share_public.SinaQuoteClient.fetch_quote``。
#:
#: R35：这里原来钉的是**东财 push2** 的响应，而那个端点在本机（大阪）实测 502，
#: 所以那套 stub 从上线起钉的是一个**在生产上永远 unavailable** 的依赖。源已换成
#: 新浪快照（见 ``a_share_snapshot._attach_dual_compare``）。
_QUOTE_OK: dict = {
    "code": "sh600519",
    "name": "贵州茅台",
    "open": 98.0,
    "prev_close": 99.0,
    "last": 100.0,
    "high": 105.0,
    "low": 95.0,
    "volume": 3833098.0,
    "date": "2026-10-02",
    "time": "16:14:58",
}


@pytest.fixture
def stub_realtime_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    """把上游实时报价钉成固定值，让用例**不发外网请求**。

    ## 为什么需要

    ``_attach_dual_compare`` 在**每次** ``build_ashare_snapshot`` 里都会取一次上游
    实时报价，而它是快照路径里**唯一**的外网调用。代价有两层：一是套件变慢且受
    外网抖动影响；二是**断言两次快照相等**的用例会随机红 —— 两次调用里只要可达性
    不一致就炸。实测在 CI 上就是这样红的::

        {'dual_compare': {'available': True,  'realtime_price': 1258.62, ...}}
        !=
        {'dual_compare': {'available': False, 'reason': 'realtime_unavailable'}}

    ## 为什么现在可以窄得多（R35）

    旧实现把 ``urllib.request.urlopen`` **整个换掉**，所以只能做成 opt-in ——
    ``served()`` 起的是真 HTTP server，全局替换会把那些真调用一起打死。
    现在只 patch ``SinaQuoteClient.fetch_quote`` 这一个方法，**碰不到**任何
    ``urlopen`` 调用，风险面小得多（保留 opt-in 以便逐文件显式声明）。

    用法::

        pytestmark = pytest.mark.usefixtures("stub_realtime_quote")
    """
    from cpt.adapters.a_share_public import SinaQuoteClient

    monkeypatch.setattr(SinaQuoteClient, "fetch_quote", lambda self, code, **kw: dict(_QUOTE_OK))


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
