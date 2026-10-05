"""``parity_reference`` 的降级契约（R45 补）。

## 为什么现在才补

R45 第五轮全量扫描实测：本模块在测试里**零覆盖**。
而 R45 同一天**刚重构过它** —— 原来参照侧是本模块里的两个私有函数
（``_czsc_structures`` / ``_tencent_structures``），被改成**委派给
``reference`` 后端**。

⇒ 那次重构改的是**每次 A 股快照都会走**的路径，**却没有任何测试**。
风险是我自己引入的，本文件把它补上。

## 钉住什么

1. **永不抛异常** —— 本函数在快照构造路径上，抛了会带崩整张快照。
   函数 docstring 第一句就是「**永不抛异常**」。
2. **两条参照路都拿不到 ⇒ ``available: false`` + 写明原因**，
   ���**不给空壳**。宁可说「没参照」，也不要一个看上去齐全、
   实际两边都是空数组的对照块 —— 后者会被前端画成「全部一致」。
3. **用了哪一级必须如实写进 ``reference``**（czsc / tencent_hfq），
   前端标签就是取自这个字段（见 ``dashboard.js::sideLabel``）。

全程离线：不碰 DB、不联网 —— 参照后端整体替换成假实现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.adapters.reference_backend import (  # noqa: E402
    ReferenceChanlunBackend,
    ReferenceUnavailableError,
)
from cpt.adapters.reference_chanlun import ReferenceChanlunConfig  # noqa: E402
from cpt.application import parity_reference as pr  # noqa: E402


class _FakeBackend(ReferenceChanlunBackend):
    """假参照后端：只回报 source/detail，不做真实计算。"""

    def __init__(self, *, source: str | None, raises: bool = False) -> None:
        super().__init__(code="600519")
        self._source = source
        self._raises = raises
        self.called_with: int | None = None

    def compute_domain_structures(self, bars, config):  # type: ignore[no-untyped-def]
        # ⚠️ 钩子是 ``compute_domain_structures``（parity 层调的是它，
        # 不是契约方法 ``compute_structures``）—— 第一版覆盖错了方法，
        # 于是真的那个跑起来，czsc 可用，断言拿到 'czsc'。
        self.called_with = len(bars)
        if self._raises:
            raise ReferenceUnavailableError("czsc 未安装且腾讯 hfq 取不到")
        self.source = self._source
        self.detail = f"假实现 · {self._source}"
        return ([], [], [])


@pytest.fixture
def patch_backend(monkeypatch: pytest.MonkeyPatch):
    def _install(fake: ReferenceChanlunBackend) -> ReferenceChanlunBackend:
        import cpt.adapters.backend_factory as bf

        monkeypatch.setattr(bf, "resolve_backend", lambda *a, **k: fake)
        return fake

    return _install


def _config_fields(cls_name: object) -> set[str]:
    from dataclasses import fields

    assert cls_name == ReferenceChanlunConfig.__name__
    return {f.name for f in fields(ReferenceChanlunConfig)}


def _call() -> dict:
    return pr.build_parity_snapshot_for(
        code="600519",
        bars=[],
        fractals=[],
        bis=[],
        zhongshus=[],
        production_backend=object(),
    )


def test_unavailable_reports_reason_and_no_shell(patch_backend) -> None:
    """两条路都拿不到 ⇒ available:False + 写明原因，**不给空壳**。"""
    patch_backend(_FakeBackend(source=None, raises=True))
    out = _call()
    assert out["available"] is False
    assert out["reason"] in ("reference_unavailable", "reference_error")
    assert out["reference"]["source"] == "none"  # payload 里 reference 是 dict
    assert "czsc" in out["reference"]["detail"]


def test_never_raises_even_if_backend_explodes(patch_backend) -> None:
    """⚠️ 核心：即使后端抛了别的异常，也**不能**带崩快照构造。"""
    fake = _FakeBackend(source="czsc", raises=True)

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("czsc 内部炸了")

    fake.compute_domain_structures = _boom  # type: ignore[method-assign]
    patch_backend(fake)
    out = _call()  # 不应抛
    assert out["available"] is False
    assert out["reason"] == "reference_error"
    # 必须**如实写出异常类型**，不能吞成一个空壳
    assert "RuntimeError" in out["reference"]["detail"]


def test_reports_actual_source_used(patch_backend) -> None:
    """用了哪一级必须如实回报 —— 前端标签就取自这个字段。"""
    patch_backend(_FakeBackend(source="tencent_hfq"))
    out = _call()
    assert out["available"] is True
    # ⚠️ payload 的 ``reference`` 是 **dict** {"source","detail"}，不是字符串
    # （实测 build_parity_snapshot 会把两个入参组装成 dict）。
    # 第一版断言写成 ``== "tencent_hfq"`` ⇒ 把**正确的实现**判成错的。
    assert out["reference"]["source"] == "tencent_hfq"


def test_czsc_detail_mentions_production_backend(patch_backend) -> None:
    patch_backend(_FakeBackend(source="czsc"))
    out = _call()
    assert out["available"] is True
    assert out["reference"]["source"] == "czsc"
    # detail 要说清跟谁比（面板标签就取自 source）
    assert "czsc" in out["reference"]["detail"]


def test_backend_gets_config_pinned_to_min_bi_len(monkeypatch) -> None:
    """委派时必须把口径传下去 —— 漏传会让参照侧用不同阈值比，结论作废。"""
    from cpt.domain.config import RulesConfig

    seen: dict[str, object] = {}

    class _Probe(ReferenceChanlunBackend):
        def compute_domain_structures(self, bars, config):  # type: ignore[no-untyped-def]
            seen["config_type"] = type(config).__name__
            self.source = "czsc"
            # ⚠️ 必须返回**三元组**，不是 ChanlunResult ——
            # 我把方法名从 compute_structures 改成 compute_domain_structures 时
            # 忘了改返回值，于是 parity 层 `ref[0]` 拿到 ChanlunResult 崩掉。
            # 同一个错法连犯两次（假后端钩子、返回值）。
            return ([], [], [])

    import cpt.adapters.backend_factory as bf

    def _fake_resolve(*a, **k):  # type: ignore[no-untyped-def]
        seen["min_bi_len"] = k.get("min_bi_len")
        return _Probe()

    monkeypatch.setattr(bf, "resolve_backend", _fake_resolve)
    cfg = RulesConfig()
    pr.build_parity_snapshot_for(
        code="600519",
        bars=[],
        fractals=[],
        bis=[],
        zhongshus=[],
        production_backend=object(),
        config=cfg,
    )
    # ⚠️ min_bi_len 必须走 resolve_backend，**不能**塞进 ReferenceChanlunConfig
    # （那个 dataclass 没有该字段，会 TypeError 且不在 except 覆盖范围内）
    assert seen["min_bi_len"] == cfg.min_bi_len
    assert "min_bi_len" not in _config_fields(seen["config_type"]), (
        "又往 ReferenceChanlunConfig 上塞 min_bi_len 了 —— 那是 TypeError 源头"
    )
