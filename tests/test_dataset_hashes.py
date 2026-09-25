"""dataset_hash 的规范化回归测试（原「审计报告测试缺口 + 问题 9」）。

覆盖测试缺口第 4 条：``dataset_hash`` 对 bars / bis 的**列表顺序敏感**、对 dict
**键序不敏感**（配合问题 8 的 docstring，即 ``sort_keys=True`` 的预期行为）。

原文件中 ``load_raw_bars`` 半开区间边界与 ``structure_events`` 索引两部分测试，
随 ``cpt/storage/`` 整层一起移除（2026-09-25 审核 P0-2，见
``docs/audit/cpt-code-audit-20260925.md`` §3.4）。
"""

from __future__ import annotations

from cpt.application.export import dataset_hash, export_dataset
from cpt.domain.config import default_rules_config
from cpt.domain.models import make_canonical_bar


def _bar(t: int) -> object:
    return make_canonical_bar(
        open_time=t, close_time=t + 600, open=100.0, high=101.0, low=99.0, close=100.5
    )


# ===== 测试缺口第 4 条: dataset_hash 对 bar 顺序敏感 =====


def test_dataset_hash_sensitive_to_bar_order() -> None:
    """问题 8 docstring 文档化: 列表顺序参与哈希。"""
    cfg = default_rules_config()
    b1 = _bar(1000)
    b2 = _bar(2000)
    payload_fwd = export_dataset(
        config=cfg,
        bars=[b1, b2],
        fractals=[],
        bis=[],
        zhongshus=[],
        events=[],
        signals=[],
    )
    payload_rev = export_dataset(
        config=cfg,
        bars=[b2, b1],
        fractals=[],
        bis=[],
        zhongshus=[],
        events=[],
        signals=[],
    )
    # 同一组 bars 顺序不同 → 不同 hash
    assert dataset_hash(payload_fwd) != dataset_hash(payload_rev), (
        "dataset_hash 应受 bars 顺序影响(测试缺口第 4 条 + 问题 8)"
    )


def test_dataset_hash_sensitive_to_bi_order() -> None:
    """Bi 顺序同样影响 hash。"""
    from cpt.adapters.reference_chanlun import BiRaw, map_bi

    cfg = default_rules_config()
    bar = _bar(1000)
    bi_a = map_bi(
        BiRaw(direction=1, start_bar=0, end_bar=1, high=110.0, low=99.0, level=0),
        level=5,
        source_ids=("a",),
        bars=[bar, bar],
    )
    bi_b = map_bi(
        BiRaw(direction=-1, start_bar=1, end_bar=2, high=110.0, low=99.0, level=0),
        level=5,
        source_ids=("b",),
        bars=[bar, bar, bar],
    )
    p1 = export_dataset(
        config=cfg, bars=[bar], fractals=[], bis=[bi_a, bi_b], zhongshus=[], events=[], signals=[]
    )
    p2 = export_dataset(
        config=cfg, bars=[bar], fractals=[], bis=[bi_b, bi_a], zhongshus=[], events=[], signals=[]
    )
    assert dataset_hash(p1) != dataset_hash(p2)


def test_dataset_hash_independent_of_dict_key_order() -> None:
    """dict 键序不影响 hash (sort_keys=True 的预期行为)。"""
    cfg = default_rules_config()
    bar = _bar(1000)
    payload = export_dataset(
        config=cfg,
        bars=[bar],
        fractals=[],
        bis=[],
        zhongshus=[],
        events=[],
        signals=[],
    )
    # 重写 metadata 字典, 键序不同
    payload_alt = dict(payload)
    payload_alt["metadata"] = {
        k: payload["metadata"][k] for k in reversed(list(payload["metadata"]))
    }
    payload_alt["data"] = {k: payload["data"][k] for k in reversed(list(payload["data"]))}
    assert dataset_hash(payload) == dataset_hash(payload_alt), "sort_keys=True 应让 dict 键序规范化"


# ===== schema v1 冻结守卫: bar 对象键集合不得静默变化 =====


#: schema v1 的 bar 对象键集合（12 个 ``CanonicalBar`` 字段 + 派生 ``direction``）。
#: 与 ``docs/export-schema-v1.md`` §「bars」表逐字一致。
_FROZEN_BAR_KEYS = frozenset(
    {
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "trade_count",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "is_closed",
        "direction",
    }
)


def test_export_bar_keys_are_frozen_schema_v1() -> None:
    """``export_dataset()`` 产出的 bar 键集合必须等于冻结集合。

    这条守卫是 F3-② 合并 ``_bar_to_dict``（现 ``cpt.application._bar_dict.bar_to_dict``）
    的配套：导出与看板从此共用同一个投影函数，看板侧"顺手加个字段"会**同时**改掉
    导出格式。这里把导出侧的键集合钉死，任何静默漂移都会红，逼改动者显式升
    ``EXPORT_SCHEMA_VERSION`` 并同步 ``docs/export-schema-v1.md``。
    """
    payload = export_dataset(
        config=default_rules_config(),
        bars=[_bar(1000)],
        fractals=[],
        bis=[],
        zhongshus=[],
        events=[],
        signals=[],
    )
    bars = payload["data"]["bars"]
    assert len(bars) == 1
    assert set(bars[0]) == _FROZEN_BAR_KEYS, (
        "schema v1 的 bar 键集合变了：若是有意改动，请升 EXPORT_SCHEMA_VERSION "
        "并同步 docs/export-schema-v1.md 与本测试"
    )
