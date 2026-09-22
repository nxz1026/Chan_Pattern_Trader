"""审计报告测试缺口 + 问题 9 的回归测试。

覆盖:
- 测试缺口第 2 条: load_raw_bars start_ms/end_ms 半开区间边界 (off-by-one)
- 测试缺口第 4 条: dataset_hash 对 bar 顺序敏感 (配合问题 8 docstring)
- 问题 9: limit 参数 + structure_events 索引
"""

from __future__ import annotations

import pytest
from cpt.application.export import dataset_hash, export_dataset
from cpt.domain.config import default_rules_config
from cpt.domain.models import (
    StructureEvent,
    StructureState,
    make_canonical_bar,
)
from cpt.storage.repository import SQLiteRepository

# ===== 测试缺口第 2 条: 半开区间边界 =====


def _bar(t: int) -> object:
    return make_canonical_bar(
        open_time=t, close_time=t + 600, open=100.0, high=101.0, low=99.0, close=100.5
    )


def test_load_raw_bars_half_open_inclusive_start() -> None:
    """[start_ms, end_ms) 半开: start_ms 包含。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    bars = [_bar(1000), _bar(2000), _bar(3000), _bar(4000)]
    repo.upsert_raw_bars(bars, symbol="BTCUSDT", interval_minutes=5, fetched_at=1)

    loaded = repo.load_raw_bars(
        symbol="BTCUSDT",
        interval_minutes=5,
        start_ms=2000,
        end_ms=4000,
    )
    # 应包含 start_ms=2000, 不含 end_ms=4000
    times = [b.open_time for b in loaded]
    assert times == [2000, 3000], f"半开区间应为 [2000, 4000), 实测 {times}"


def test_load_raw_bars_half_open_exclusive_end() -> None:
    """[start_ms, end_ms) 半开: end_ms 不包含。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    bars = [_bar(1000), _bar(2000), _bar(3000)]
    repo.upsert_raw_bars(bars, symbol="BTCUSDT", interval_minutes=5, fetched_at=1)

    loaded = repo.load_raw_bars(
        symbol="BTCUSDT",
        interval_minutes=5,
        start_ms=2000,
        end_ms=3000,
    )
    assert [b.open_time for b in loaded] == [2000]  # 3000 不含


def test_load_raw_bars_no_range_returns_all() -> None:
    """不传时间范围应返回全部。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    bars = [_bar(1000), _bar(2000), _bar(3000)]
    repo.upsert_raw_bars(bars, symbol="BTCUSDT", interval_minutes=5, fetched_at=1)

    loaded = repo.load_raw_bars(symbol="BTCUSDT", interval_minutes=5)
    assert len(loaded) == 3


def test_load_raw_bars_limit_param() -> None:
    """limit 参数限制返回条数(按时间最早)。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    bars = [_bar(1000), _bar(2000), _bar(3000), _bar(4000)]
    repo.upsert_raw_bars(bars, symbol="BTCUSDT", interval_minutes=5, fetched_at=1)

    loaded = repo.load_raw_bars(symbol="BTCUSDT", interval_minutes=5, limit=2)
    assert [b.open_time for b in loaded] == [1000, 2000]


def test_load_raw_bars_limit_must_be_positive() -> None:
    """limit<=0 应 raise ValueError 而非静默返回 0 条。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    with pytest.raises(ValueError, match="正整数"):
        repo.load_raw_bars(symbol="BTCUSDT", interval_minutes=5, limit=0)
    with pytest.raises(ValueError, match="正整数"):
        repo.load_raw_bars(symbol="BTCUSDT", interval_minutes=5, limit=-5)


def test_list_structure_events_limit_param() -> None:
    """list_structure_events 支持 limit, 取最早 N 条。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    repo.upsert_structure_state(
        StructureState(
            id="p1",
            level=5,
            kind="bi",
            direction=1,
            start_time=1000,
            end_time=2000,
            status="forming",
            revision=1,
            first_seen_at=1_700_000_000_000,
            confirmed_at=None,
            invalidated_at=None,
            source_ids=("f1",),
        )
    )
    for i in range(5):
        repo.append_structure_event(
            StructureEvent(
                event_type="updated",
                structure_id="p1",
                revision=i + 1,
                payload={"step": i},
                occurred_at=1_700_000_000_000 + i,
            )
        )

    all_events = repo.list_structure_events("p1")
    assert len(all_events) == 5

    limited = repo.list_structure_events("p1", limit=2)
    assert len(limited) == 2
    assert limited[0].revision == 1
    assert limited[1].revision == 2


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


# ===== 问题 9: structure_events 索引生效 =====


def test_structure_events_index_exists() -> None:
    """DDL 必须创建 idx_events_sid_time 复合索引。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    rows = repo._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_events_sid_time'"
    ).fetchall()
    assert rows, "idx_events_sid_time 索引未创建"
    repo.close()


def test_structure_events_index_used_for_query() -> None:
    """EXPLAIN QUERY PLAN 应使用 idx_events_sid_time 索引(不是 SCAN)。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    repo.upsert_structure_state(
        StructureState(
            id="p1",
            level=5,
            kind="bi",
            direction=1,
            start_time=1000,
            end_time=2000,
            status="forming",
            revision=1,
            first_seen_at=1_700_000_000_000,
            confirmed_at=None,
            invalidated_at=None,
            source_ids=("f1",),
        )
    )
    plan = repo._conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM structure_events WHERE structure_id = ? "
        "ORDER BY occurred_at ASC",
        ("p1",),
    ).fetchall()
    plan_text = " ".join(row[3] for row in plan)
    assert "idx_events_sid_time" in plan_text, f"未使用索引, plan: {plan_text!r}"
    repo.close()
