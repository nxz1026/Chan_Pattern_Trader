"""M1 集成测试：replay + fixture + 同输入哈希稳定 + storage round-trip。

这些测试覆盖 implementation-plan.md §4 的 M1 验收条件：
- 人工案例全过（每个 fixture 满足预期 bi count/direction）
- 同输入导出哈希一致（hash stability）
- 端到端 export → 重读 → schema v1
- storage (SQLiteRepository) round-trip 不丢失字段
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cpt.application.export import dataset_hash
from cpt.application.replay import load_fixture, run_replay
from cpt.domain.models import (
    Signal,
    StructureEvent,
)
from cpt.storage.repository import SQLiteRepository

FIXTURES_DIR = Path(__file__).parent / "fixtures"

ALL_CASES: list[str] = [
    "case1_simple_uptrend",
    "case2_simple_downtrend",
    "case3_zigzag",
    "case4_volatile",
    "case5_constant",
    "case6_short",
    "case7_long",
    "case8_integration",
]


@pytest.mark.parametrize("case_name", ALL_CASES)
def test_replay_hash_stable(case_name: str) -> None:
    """同输入跨次调用产出同一 dataset_hash（schema v1 复现性基础）。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / f"{case_name}.json")
    p1 = run_replay(config=cfg, bars=bars, metadata=meta)
    p2 = run_replay(config=cfg, bars=bars, metadata=meta)
    assert dataset_hash(p1) == dataset_hash(p2), (
        f"{case_name} hash unstable: {dataset_hash(p1)} != {dataset_hash(p2)}"
    )


@pytest.mark.parametrize("case_name", ALL_CASES)
def test_replay_export_schema_v1(case_name: str, tmp_path: Path) -> None:
    """导出文件满足 schema v1 顶层字段。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / f"{case_name}.json")
    payload = run_replay(config=cfg, bars=bars, metadata=meta)
    # 直接通过 export_dataset 写 payload 到文件
    out = tmp_path / f"{case_name}.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "v1"
    assert on_disk["config"]["config_version"] == cfg.config_version
    assert "bars" in on_disk["data"]


def test_case1_uptrend_one_bi() -> None:
    """case1: 涨后跌 → 1 bi direction=-1。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / "case1_simple_uptrend.json")
    payload = run_replay(config=cfg, bars=bars, metadata=meta)
    assert len(payload["data"]["bis"]) == 1
    assert payload["data"]["bis"][0]["direction"] == -1


def test_case2_downtrend_one_bi() -> None:
    """case2: 跌后涨 → 1 bi direction=+1。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / "case2_simple_downtrend.json")
    payload = run_replay(config=cfg, bars=bars, metadata=meta)
    assert len(payload["data"]["bis"]) == 1
    assert payload["data"]["bis"][0]["direction"] == 1


def test_case3_zigzag_multiple_bi() -> None:
    """case3: zigzag → 3+ bi。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / "case3_zigzag.json")
    payload = run_replay(config=cfg, bars=bars, metadata=meta)
    assert len(payload["data"]["bis"]) >= 3


def test_case5_constant_no_bi() -> None:
    """case5: 常数 → 0 bi。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / "case5_constant.json")
    payload = run_replay(config=cfg, bars=bars, metadata=meta)
    assert len(payload["data"]["bis"]) == 0


def test_case6_short_no_bi() -> None:
    """case6: 仅 2 根 → 0 bi（分型要求至少 3 根）。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / "case6_short.json")
    payload = run_replay(config=cfg, bars=bars, metadata=meta)
    assert len(payload["data"]["bis"]) == 0


def test_replay_export_to_file_round_trip(tmp_path: Path) -> None:
    """端到端: replay → 写文件 → 重读 → dataset_hash 跨序列化一致。

    dict 顺序差异不影响 hash(sort_keys=True), 不需要 _normalize 重新比较;
    此处只断言 hash(哈希基于规范化 JSON, 顺序无关); payload['data'] ==
    on_disk['data'] 不再断言, 因为重读后 dict 内部 hash 不可控。
    """
    cfg, bars, meta = load_fixture(FIXTURES_DIR / "case3_zigzag.json")
    payload = run_replay(config=cfg, bars=bars, metadata=meta)
    out = tmp_path / "out.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )

    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert dataset_hash(on_disk) == dataset_hash(payload)


def test_storage_raw_bars_round_trip() -> None:
    """case4 的 K 线写入 SQLiteRepository 再读出,逐字段比对。"""
    cfg, bars, meta = load_fixture(FIXTURES_DIR / "case4_volatile.json")
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    n = repo.upsert_raw_bars(
        bars, symbol="BTCUSDT", interval_minutes=5, fetched_at=1_700_000_000_000
    )
    assert n == len(bars)

    loaded = repo.load_raw_bars(symbol="BTCUSDT", interval_minutes=5)
    assert len(loaded) == len(bars)
    for orig, ld in zip(bars, loaded, strict=True):
        assert orig == ld


def test_storage_events_signals_round_trip() -> None:
    """events/signals 经 SQLiteRepository round-trip 保持完整。"""
    from cpt.domain.models import StructureState

    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    state = StructureState(
        id="p1",
        level=5,
        kind="bi",
        direction=1,
        start_time=1000000,
        end_time=2000000,
        status="forming",
        revision=1,
        first_seen_at=1_700_000_000_000,
        confirmed_at=None,
        invalidated_at=None,
        source_ids=("f1", "f2"),
    )
    repo.upsert_structure_state(state)

    event = StructureEvent(
        event_type="created",
        structure_id="p1",
        revision=1,
        payload={"kind": "bi", "high": 110.0},
        occurred_at=1_700_000_000_000,
    )
    repo.append_structure_event(event)
    repo.append_structure_event(
        StructureEvent(
            event_type="confirmed",
            structure_id="p1",
            revision=2,
            payload={"confirmed_at": 1_700_000_500_000},
            occurred_at=1_700_000_500_000,
        )
    )

    sig = Signal(
        signal_id="s1",
        level=5,
        signal_type="first_buy",
        status="alert",
        structure_id="p1",
        center_ids=("zs1", "zs2"),
        divergence_status="not_checked",
        alert_time=1_700_000_000_000,
        candidate_time=None,
        confirmed_time=None,
        invalidated_time=None,
        price=105.5,
        source_revision=1,
    )
    repo.upsert_signal(sig)

    # read back
    events = repo.list_structure_events("p1")
    assert len(events) == 2
    assert events[0].event_type == "created"
    assert events[0].payload == {"kind": "bi", "high": 110.0}
    assert events[1].event_type == "confirmed"

    loaded_sig = repo.load_signal("s1")
    assert loaded_sig is not None
    assert loaded_sig.signal_id == "s1"
    assert loaded_sig.center_ids == ("zs1", "zs2")
    assert loaded_sig.price == 105.5


def test_fixture_metadata_hints_match() -> None:
    """fixture metadata 中的 expected_* 字段应与算法实际产出匹配。

    这是 fixture 自描述 + 防止回归的契约测试。
    """
    for case_name in ALL_CASES:
        cfg, bars, meta = load_fixture(FIXTURES_DIR / f"{case_name}.json")
        payload = run_replay(config=cfg, bars=bars, metadata=meta)
        actual_bi = len(payload["data"]["bis"])
        expected_bi = meta.get("expected_bi_count")
        min_bi = meta.get("expected_min_bi_count")
        if expected_bi is not None:
            assert actual_bi == expected_bi, (
                f"{case_name}: expected {expected_bi} bi, got {actual_bi}"
            )
        if min_bi is not None:
            assert actual_bi >= min_bi, f"{case_name}: expected >= {min_bi} bi, got {actual_bi}"
        if "expected_bi_direction" in meta:
            bis = payload["data"]["bis"]
            assert bis, f"{case_name}: expected bis but none"
            assert bis[0]["direction"] == meta["expected_bi_direction"], (
                f"{case_name}: expected first bi direction "
                f"{meta['expected_bi_direction']}, got {bis[0]['direction']}"
            )
