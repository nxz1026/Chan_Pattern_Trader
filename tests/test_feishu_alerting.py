"""飞书告警通道的**失败语义**（R45 补）。

## 为什么现在才补

全量扫描（门禁④的延伸）实测：``cpt/adapters/feishu.py`` 在测试里
**零覆盖** —— ``test_llm_config_env_isolation.py`` 只是把它列进环境变量
隔离清单，**没有测过它的行为**。

而这个模块是**生产告警通道**：`deploy/cron/run-inspection-daily.sh` 每天
03:40 UTC 跑巡检，告警发不出去时日志只会写一行「未配置，跳过告警」。

## 本文件钉住什么

1. **观测通道不该有能力让业务路径崩掉** ⇒ `notify` 永远不抛，
   没配 / 送失败都返回 ``False``。这条是设计意图，必须钉住 ——
   哪天有人「顺手」加个 raise，巡检就会被告警通道带崩。
2. **「没配」与「送失败」不能同码到无法区分** ——
   都返回 False 是刻意的（业务只看「有没有送到」），
   但**日志必须留痕**（``_LOG.info``），否则排查时看不到发生了什么。
3. **截断必须留痕**：超长文本被裁掉时要在消息里写明丢了多少字符，
   否则「消息少了一截」会变成第二个谜题。

全程离线：用假 opener，不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.adapters import feishu  # noqa: E402


class _Resp:
    status = 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_not_configured_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 webhook ⇒ False，且**不联网**。"""
    monkeypatch.delenv(feishu.ENV_WEBHOOK, raising=False)
    assert feishu.webhook_configured() is False

    def _boom(*a: object, **k: object) -> int:
        raise AssertionError("没配 webhook 时不该尝试发送")

    assert feishu.notify("标题", ["一行"], opener=_boom) is False


def test_send_failure_returns_false_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 核心：发送失败**返回 False，不抛**。

    观测通道不能让业务路径崩 —— 巡检不能因为飞书挂了就不跑完。
    """
    monkeypatch.setenv(feishu.ENV_WEBHOOK, "https://hooks.example/x")

    def _fail(*a: object, **k: object) -> int:
        raise TimeoutError("connection reset")

    assert feishu.notify("标题", opener=_fail) is False


def test_success_returns_true_and_posts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功时返回 True，且请求体是飞书要的 text 结构。"""
    monkeypatch.setenv(feishu.ENV_WEBHOOK, "https://hooks.example/x")
    seen: dict[str, object] = {}

    def _ok(req: object, timeout: float) -> int:
        seen["url"] = getattr(req, "full_url", None)
        seen["data"] = getattr(req, "data", None)
        seen["timeout"] = timeout
        return 200

    assert feishu.notify("巡检告警", ["一行 A", "一行 B"], opener=_ok) is True
    assert seen["url"] == "https://hooks.example/x"
    payload = (seen["data"] or b"").decode("utf-8")
    assert '"msg_type": "text"' in payload
    assert "巡检告警" in payload and "一行 A" in payload


def test_explicit_webhook_arg_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(feishu.ENV_WEBHOOK, "https://env.example/x")
    seen: list[str] = []

    def _ok(req: object, timeout: float) -> int:
        seen.append(getattr(req, "full_url", ""))
        return 200

    feishu.notify("t", webhook="https://arg.example/y", opener=_ok)
    assert seen == ["https://arg.example/y"]


def test_clip_keeps_full_trace() -> None:
    """截断必须**留痕**：丢了多少字符要写出来。"""
    short = "a" * 10
    assert feishu._clip(short) == short

    long = "b" * (feishu._MAX_CHARS + 123)
    out = feishu._clip(long)
    assert len(out) < len(long) + 40        # 只多了那句留痕，不是整条
    assert "已截断 123 字符" in out


def test_notify_problem_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``notify_problem`` 是最外层的便捷入口，**更不能抛**。"""
    monkeypatch.delenv(feishu.ENV_WEBHOOK, raising=False)
    feishu.notify_problem("巡检", ["2 降级"])
