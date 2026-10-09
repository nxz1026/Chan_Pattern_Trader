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

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.adapters import feishu  # noqa: E402


class _Resp:
    status = 200

    def __enter__(self) -> _Resp:
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
    assert len(out) < len(long) + 40  # 只多了那句留痕，不是整条
    assert "已截断 123 字符" in out


def test_notify_problem_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``notify_problem`` 是最外层的便捷入口，**更不能抛**。

    R59（审计 L13）：原来这里只写了「调用它」这个动作 —— 把函数体删空照样绿。
    现在断言**可观测结果**：没配 webhook 时明确回 ``False``（不是 ``None``、
    不是抛异常），配好且确认送达时明确回 ``True``。
    """
    monkeypatch.delenv(feishu.ENV_WEBHOOK, raising=False)
    assert feishu.notify_problem("巡检", ["2 降级"]) is False

    delivered = feishu.notify_problem(
        "巡检", ["2 降级"], webhook=_WEBHOOK, opener=_opener(200, '{"code": 0}')
    )
    assert delivered is True


# --------------------------------------------------------------------------- #
# R59（审计 H8 / M9）：**HTTP 200 不等于送达** + token 不许进日志
# --------------------------------------------------------------------------- #
#
# 审计发现：原实现只判 HTTP status，而注释自己就写着「飞书失败时也返回 200 +
# errcode」。于是 webhook 失效、限流（9499）、关键词不匹配（19024）这些**真实
# 失败**全被记成「已送达」，告警静默丢失 —— 而告警通道静默丢失等于没有告警。
#
# 同时 ``ValueError: unknown url type: '<完整 URL>'`` 会把 hook token 原文打进
# journal（token 就是群里的写权限），所以落日志前必须抹掉。

_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/SECRET-TOKEN-abc123"


class _BodyResp:
    """带 body 的假响应 —— 飞书的判定信息全在 body 里。"""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def __enter__(self) -> _BodyResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _opener(status: int, body: str):  # noqa: ANN202 — 测试局部替身
    def _send(req: object, timeout: float) -> tuple[int, str]:
        return status, body

    return _send


def test_http_200_with_error_code_is_not_delivered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """⚠️ 核心（H8）：200 + ``code!=0`` **必须**判未送达。"""
    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)
    rejected = _opener(200, '{"code": 9499, "msg": "Bad Request"}')
    with caplog.at_level(logging.INFO):
        sent = feishu.notify("标题", ["一行"], opener=rejected)

    assert sent is False
    assert "已送达" not in caplog.text, caplog.text
    assert "未送达" in caplog.text and "9499" in caplog.text
    assert "Bad Request" in caplog.text  # 飞书的原话要留着，否则排查无处下手


@pytest.mark.parametrize(
    "body",
    [
        '{"errcode": 19024, "msg": "Key Words Not Found"}',
        '{"StatusCode": 1, "StatusMessage": "auth failed"}',
        '{"code": "9499"}',  # 字符串数字也算，别被类型差异放过
    ],
)
def test_other_result_fields_are_also_checked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, body: str
) -> None:
    """不同网关/版本的字段名不同（``code``/``errcode``/``StatusCode``），都要看。"""
    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)
    with caplog.at_level(logging.INFO):
        assert feishu.notify("标题", opener=_opener(200, body)) is False
    assert "已送达" not in caplog.text


def test_http_200_with_code_zero_is_delivered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``code=0`` 才是真的送达 —— 修完必须还能认出成功，否则就是反向误报。"""
    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)
    with caplog.at_level(logging.INFO):
        assert feishu.notify("标题", opener=_opener(200, '{"code": 0, "msg": "success"}')) is True
    assert "已送达" in caplog.text


def test_non_json_body_is_treated_as_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """200 + 非 JSON body（反代返回 HTML 错误页）按**未送达**处理：宁可误报也别漏报。"""
    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)
    with caplog.at_level(logging.INFO):
        assert feishu.notify("标题", opener=_opener(200, "<html>502 Bad Gateway</html>")) is False
    assert "不是 JSON" in caplog.text


def test_empty_body_keeps_old_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 body = 无从判断，保持旧口径（否则所有老替身/裸 200 都会翻红）。"""
    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)
    assert feishu.notify("标题", opener=_opener(200, "")) is True


def test_legacy_int_opener_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """老替身只回状态码 —— 兼容，但要显式记下"此时校验不到 body 语义"。"""

    def _legacy(req: object, timeout: float) -> int:
        return 200

    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)
    assert feishu.notify("标题", opener=_legacy) is True


def test_default_opener_actually_reads_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ H8 的病灶在**默认** opener：替身回 tuple 不算数，必须验真身也读 body。"""
    seen: dict[str, object] = {}

    def _fake_urlopen(req: object, timeout: float = 0.0) -> _BodyResp:
        seen["url"] = getattr(req, "full_url", None)
        return _BodyResp(200, '{"code": 19024, "msg": "Key Words Not Found"}')

    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)
    monkeypatch.setattr(feishu.urllib.request, "urlopen", _fake_urlopen)

    assert feishu.notify("标题") is False  # 默认 opener 也必须认出 errcode
    assert seen["url"] == _WEBHOOK


def test_hook_token_never_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """⚠️ M9：异常原文里的完整 webhook（含 token）必须被抹掉。"""
    monkeypatch.setenv(feishu.ENV_WEBHOOK, _WEBHOOK)

    def _boom(req: object, timeout: float) -> tuple[int, str]:
        raise ValueError(f"unknown url type: '{_WEBHOOK}'")

    with caplog.at_level(logging.WARNING):
        assert feishu.notify("标题", opener=_boom) is False

    assert "SECRET-TOKEN-abc123" not in caplog.text, caplog.text
    assert "unknown url type" in caplog.text  # 原因还要留着
    assert "<webhook>" in caplog.text


def test_redact_also_scrubs_a_different_hook_url() -> None:
    """异常里带的 URL 未必等于本次配置的那个（比如重定向/拼接）⇒ 按形状抹。"""
    out = feishu._redact("failed: https://other.example/bot/v2/hook/OTHER-TOKEN?x=1")
    assert "OTHER-TOKEN" not in out
    assert "/hook/<redacted>" in out


def test_body_error_reports_unparseable_code() -> None:
    """``code`` 是怪值时按失败处理并说明，而不是静默放过。"""
    assert feishu._body_error('{"code": null}') is not None
    assert feishu._body_error('{"code": 0}') is None
    assert feishu._body_error("[]") is not None
