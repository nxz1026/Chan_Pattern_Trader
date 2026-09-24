"""czsc 后端测试（R14）。

分两层：

- **不依赖 czsc 的测试**（报错路径、周期推断、方向映射）—— 始终运行，
  保证核心 ``dependencies = []`` 的前提下这些纯逻辑仍被覆盖。
- **依赖 czsc 的测试** —— 用 ``pytest.importorskip`` 跳过。它们是
  R14 的核心回归：CPT 自研笔端点中位跨度只有 2 根原始K线（66–74% 的笔
  跨度 < 4 根，两根 3K 分型窗口重叠，定义上不可能成笔），换成 czsc 后
  必须回到正常量级。
"""

from __future__ import annotations

import csv
import tomllib
from pathlib import Path

import pytest
from cpt.adapters.czsc_chanlun import (
    DEFAULT_MIN_BI_LEN,
    PINNED_CZSC_VERSION,
    CzscChanlunBackend,
    CzscNotInstalledError,
    CzscVersionError,
    _bi_direction,
    _fx_kind,
    _import_czsc,
    _infer_interval_label,
)
from cpt.adapters.reference_chanlun import (
    ReferenceChanlunConfig,
    map_bi,
    map_fractal,
    map_zhongshu,
)
from cpt.domain.models import make_canonical_bar

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "oracle"
BAR_MS_5M = 300_000
CONFIG = ReferenceChanlunConfig()


def _load_fixture(path: Path) -> list:
    """读 oracle CSV → ``CanonicalBar`` 列表。"""
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            make_canonical_bar(
                open_time=int(row["open_time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                close_time=int(row["close_time"]),
                volume=float(row["volume"]),
                quote_volume=float(row["quote_volume"]),
                trade_count=int(row["trade_count"]),
                taker_buy_base_volume=float(row["taker_buy_base_volume"]),
                taker_buy_quote_volume=float(row["taker_buy_quote_volume"]),
            )
            for row in csv.DictReader(handle)
        ]


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.csv"))


# --------------------------------------------------------------------------- #
# 不依赖 czsc：纯逻辑
# --------------------------------------------------------------------------- #


def test_pinned_version_matches_pyproject_extra() -> None:
    """适配器固定的 czsc 版本必须与 ``pyproject.toml`` 的 ``chan`` extra 一致。"""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    chan_extra = data["project"]["optional-dependencies"]["chan"]
    assert chan_extra == [f"czsc=={PINNED_CZSC_VERSION}"], chan_extra


def test_core_dependencies_stay_empty() -> None:
    """接入 czsc 不得污染核心依赖（方案 C 的核心约束）。"""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["project"]["dependencies"] == []


def test_missing_czsc_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """未安装 czsc 时给出可操作的安装提示，而不是裸 ModuleNotFoundError。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "czsc":
            raise ModuleNotFoundError("No module named 'czsc'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(CzscNotInstalledError, match=r"\[chan\]"):
        _import_czsc()


def test_bi_direction_accepts_enum_and_label() -> None:
    class _Enum:
        def __init__(self, name: str) -> None:
            self.name = name

    assert _bi_direction(_Enum("Up")) == 1
    assert _bi_direction(_Enum("Down")) == -1
    assert _bi_direction("向上") == 1
    assert _bi_direction("向下") == -1
    with pytest.raises(ValueError, match="无法识别"):
        _bi_direction("sideways")


def test_fx_kind_accepts_enum_and_label() -> None:
    class _Enum:
        def __init__(self, name: str) -> None:
            self.name = name

    assert _fx_kind(_Enum("G")) == "top"
    assert _fx_kind(_Enum("D")) == "bottom"
    assert _fx_kind("顶") == "top"
    assert _fx_kind("底") == "bottom"
    with pytest.raises(ValueError, match="无法识别"):
        _fx_kind("middle")


def test_infer_interval_label_from_median_gap() -> None:
    """用中位间隔推断周期：单个巨大缺口（停牌/断线）不得带偏结论。"""
    bars = [
        make_canonical_bar(
            open_time=1_700_000_000_000 + i * BAR_MS_5M,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            close_time=1_700_000_000_000 + i * BAR_MS_5M + 299_999,
        )
        for i in range(20)
    ]
    assert _infer_interval_label(bars) == "5m"

    daily = [
        make_canonical_bar(
            open_time=1_700_000_000_000 + i * 86_400_000,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            close_time=1_700_000_000_000 + i * 86_400_000 + 86_399_999,
        )
        for i in range(20)
    ]
    assert _infer_interval_label(daily) == "1d"

    with pytest.raises(ValueError, match="至少需要 2 根"):
        _infer_interval_label(bars[:1])


def test_rejects_non_positive_min_bi_len() -> None:
    with pytest.raises(ValueError, match="min_bi_len"):
        CzscChanlunBackend(min_bi_len=0)


def test_default_min_bi_len_is_czsc_upstream_default() -> None:
    """门槛直接采用 czsc 上游默认 6，不自造数值（实测 4–7 区间笔数不敏感）。"""
    assert DEFAULT_MIN_BI_LEN == 6


# --------------------------------------------------------------------------- #
# 依赖 czsc：真实 fixture 回归
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def czsc_module() -> object:
    return pytest.importorskip("czsc")


def test_real_fixture_bi_spacing_is_definitionally_possible(czsc_module: object) -> None:
    """核心回归：笔端点跨度中位数必须回到正常量级。

    CPT 自研笔在同样 3 个 fixture 上的中位跨度是 **2 根**原始K线、
    66–74% 的笔跨度 < 4 根 —— 两根 3K 分型窗口重叠，按缠论定义不可能成笔。
    换成 czsc 后中位跨度必须 ≥ 6 根。
    """
    for path in _fixture_paths():
        bars = _load_fixture(path)
        result = CzscChanlunBackend().compute_structures(bars, CONFIG)
        bis = [
            map_bi(b, level=0, source_ids=(f"bi:{i}",), bars=bars)
            for i, b in enumerate(result.bi_list)
        ]
        assert len(bis) >= 10, f"{path.name}: 笔太少（{len(bis)}）"
        spacings = sorted(
            (bis[i + 1].start_time - bis[i].start_time) // BAR_MS_5M for i in range(len(bis) - 1)
        )
        median = spacings[len(spacings) // 2]
        assert median >= 6, f"{path.name}: 笔端点中位跨度只有 {median} 根"


def test_real_fixture_zhongshu_requires_at_least_three_bis(czsc_module: object) -> None:
    """CPT 自研中枢的约束：每个中枢至少吸收 3 笔，且严格 ``high > low``。

    这正是**不采用** czsc ``zs_list`` 的原因——它会产出 <3 笔的"中枢"。
    """
    for path in _fixture_paths():
        bars = _load_fixture(path)
        result = CzscChanlunBackend().compute_structures(bars, CONFIG)
        assert result.zs_list, f"{path.name}: 未产出任何中枢"
        for zs in result.zs_list:
            assert len(zs.bi_indices) >= 3, f"{path.name}: 中枢只吸收 {len(zs.bi_indices)} 笔"
            assert zs.high > zs.low, f"{path.name}: 中枢区间非正 (high={zs.high}, low={zs.low})"
            assert zs.start_bar <= zs.end_bar


def test_real_fixture_zhongshu_never_has_negative_width(czsc_module: object) -> None:
    """对照 czsc ``zs_list``：实测其 ``is_valid()`` 全过，但存在 <3 笔的假中枢。"""
    from czsc._native import CZSC, Freq, RawBar  # noqa: PLC0415

    path = _fixture_paths()[0]
    bars = _load_fixture(path)
    raw = [
        RawBar(
            symbol="CPT",
            id=i,
            dt=__import__("datetime")
            .datetime.fromtimestamp(bar.open_time / 1000, tz=__import__("datetime").UTC)
            .replace(tzinfo=None),
            freq=Freq.F5,
            open=bar.open,
            close=bar.close,
            high=bar.high,
            low=bar.low,
            vol=bar.volume,
            amount=bar.quote_volume,
        )
        for i, bar in enumerate(bars)
    ]
    analyzer = CZSC(raw, max_bi_num=0, min_bi_len=DEFAULT_MIN_BI_LEN)
    degenerate = [zs for zs in analyzer.zs_list if len(zs.bis) < 3]
    assert degenerate, "czsc zs_list 预期含 <3 笔的假中枢（本测试记录该事实）"

    ours = CzscChanlunBackend().compute_structures(bars, CONFIG)
    assert all(len(zs.bi_indices) >= 3 for zs in ours.zs_list)


def test_real_fixture_domain_mapping_roundtrips(czsc_module: object) -> None:
    """Raw → domain 映射后时间必须可解析为真实毫秒时间戳（非占位）。"""
    path = _fixture_paths()[0]
    bars = _load_fixture(path)
    result = CzscChanlunBackend().compute_structures(bars, CONFIG)
    open_times = {bar.open_time for bar in bars}

    for i, fx in enumerate(result.fx_list):
        mapped = map_fractal(fx, level=0, source_ids=(f"fx:{i}",), bars=bars)
        assert mapped.start_time in open_times
        assert mapped.start_time == mapped.end_time
        assert mapped.kind in {"top", "bottom"}
        assert mapped.high >= mapped.low

    for i, bi in enumerate(result.bi_list):
        mapped = map_bi(bi, level=0, source_ids=(f"bi:{i}",), bars=bars)
        assert mapped.start_time in open_times
        assert mapped.end_time in open_times
        assert mapped.direction in {1, -1}

    for i, zs in enumerate(result.zs_list):
        mapped = map_zhongshu(zs, level=0, source_ids=(f"zs:{i}",), bars=bars)
        assert mapped.start_time in open_times
        assert mapped.end_time in open_times
        assert mapped.bi_ids, "中枢必须带 bi_ids 溯源"


def test_real_fixture_fractal_and_bi_counts_are_stable(czsc_module: object) -> None:
    """锁定当前计数，任何口径漂移都要显式改这个断言。"""
    expected = {
        "btcusdt_5m_2024-02-01.csv": (202, 50),
        "btcusdt_5m_2024-09-01.csv": (203, 49),
        "btcusdt_5m_2025-04-01.csv": (245, 49),
    }
    for name, (fx_count, bi_count) in expected.items():
        bars = _load_fixture(FIXTURE_DIR / name)
        result = CzscChanlunBackend().compute_structures(bars, CONFIG)
        assert (len(result.fx_list), len(result.bi_list)) == (fx_count, bi_count), name


def test_min_bi_len_gate_actually_bites(czsc_module: object) -> None:
    """门槛调大必须减少笔数（8 起实测开始生效）。"""
    bars = _load_fixture(FIXTURE_DIR / "btcusdt_5m_2024-02-01.csv")
    loose = CzscChanlunBackend(min_bi_len=6).compute_structures(bars, CONFIG)
    strict = CzscChanlunBackend(min_bi_len=10).compute_structures(bars, CONFIG)
    assert len(strict.bi_list) < len(loose.bi_list)


def test_explicit_freq_label_bypasses_inference(czsc_module: object) -> None:
    """显式周期标签与推断结果一致。"""
    bars = _load_fixture(FIXTURE_DIR / "btcusdt_5m_2024-02-01.csv")
    inferred = CzscChanlunBackend().compute_structures(bars, CONFIG)
    explicit = CzscChanlunBackend(freq_label="5m").compute_structures(bars, CONFIG)
    assert inferred.bi_list == explicit.bi_list


def test_unsupported_freq_label_raises(czsc_module: object) -> None:
    bars = _load_fixture(FIXTURE_DIR / "btcusdt_5m_2024-02-01.csv")
    with pytest.raises(ValueError, match="无对应的 czsc Freq"):
        CzscChanlunBackend(freq_label="1M").compute_structures(bars, CONFIG)


def test_short_input_returns_empty(czsc_module: object) -> None:
    """不足 3 根时直接返回空结构，不进 czsc。"""
    bars = _load_fixture(FIXTURE_DIR / "btcusdt_5m_2024-02-01.csv")[:2]
    result = CzscChanlunBackend().compute_structures(bars, CONFIG)
    assert result == ((), (), (), {}) or (
        result.fx_list == () and result.bi_list == () and result.zs_list == ()
    )


def test_version_mismatch_raises(monkeypatch: pytest.MonkeyPatch, czsc_module: object) -> None:
    """版本不符必须响亮报错，而不是拿错版本静默出结果。"""
    import czsc as real_czsc

    monkeypatch.setattr(real_czsc, "__version__", "0.0.0-not-pinned", raising=False)
    with pytest.raises(CzscVersionError, match=PINNED_CZSC_VERSION):
        _import_czsc()
