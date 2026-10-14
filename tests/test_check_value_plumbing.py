"""`scripts/check_value_plumbing.py` 的单元测试。

每类判据都要有**两面**：抓得到已知错例、放过已知正确写法。
只证后一面（全仓零发现）与什么都没查是同一种状态 —— R61 那三个静默 bug
就是在这种状态下活下来的（页面正常、console 零报错、单测全绿）。

判据本身能不能红另由 `scripts/selftest_gates.py` 的
`check_value_plumbing.py` / `check_value_plumbing.py#R4` 两个夹具负责，
这里只管「判据的边界」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import check_value_plumbing as cvp


def _py(root: Path, rel: str, body: str) -> None:
    p = root / "cpt" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _js(root: Path, rel: str, body: str) -> None:
    p = root / "dashboard" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ── R1：除法分母守卫 ──────────────────────────────────────────
def test_r1_reports_unguarded_division(tmp_path: Path) -> None:
    _py(
        tmp_path,
        "application/probe.py",
        "def ratio(last_price, base):\n    return last_price / base\n",
    )
    rep = cvp.Report()
    assert cvp.check_divisions(tmp_path, rep) == 1
    assert [f.rule for f in rep.findings] == ["R1"]
    assert rep.errors, "R1 的发现必须是 error 级（否则 CI 放行）"
    assert "base" in rep.findings[0].why


def test_r1_silent_when_guard_present(tmp_path: Path) -> None:
    _py(
        tmp_path,
        "application/probe.py",
        "def ratio(last_price, base):\n"
        "    if not base or base <= 0:\n"
        "        return None\n"
        "    return last_price / base\n",
    )
    rep = cvp.Report()
    assert cvp.check_divisions(tmp_path, rep) == 1  # 看了一处
    assert rep.findings == []  # 但没发现


def test_r1_ignores_non_market_denominator(tmp_path: Path) -> None:
    """`/ 1000` 这类常量不该被当成行情分母 —— 假阳性会让门禁被无视。"""
    _py(
        tmp_path,
        "application/probe.py",
        "def lots_to_shares(volume):\n    return volume * 100\n"
        "\n"
        "\n"
        "def x(last_price):\n    return last_price / 1000\n",
    )
    rep = cvp.Report()
    assert cvp.check_divisions(tmp_path, rep) == 0
    assert rep.findings == []


def test_r1_ignores_compound_denominator(tmp_path: Path) -> None:
    """`/(base + 1)` 不在判据内（刻意保守：不判复合表达式，宁漏不假阳）。"""
    _py(
        tmp_path,
        "application/probe.py",
        "def x(last_price, base):\n    return last_price / (base + 1)\n",
    )
    rep = cvp.Report()
    assert cvp.check_divisions(tmp_path, rep) == 0


def test_r1_permit_silences_a_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _py(
        tmp_path,
        "application/probe.py",
        "def ratio(last_price, base):\n    return last_price / base\n",
    )
    monkeypatch.setitem(cvp._PERMIT, "cpt/application/probe.py:ratio", "上游保证 base>0（测试用）")
    rep = cvp.Report()
    assert cvp.check_divisions(tmp_path, rep) == 1
    assert rep.findings == []


def test_marketish_word_list_covers_market_names() -> None:
    for name in ("prev", "last_close", "hfq_factor", "price_ratio", "base"):
        assert cvp._MARKETISH.search(name), name


# ── R3：带符号量的格式化 ──────────────────────────────────────
def test_r3_reports_unsigned_direction_format(tmp_path: Path) -> None:
    _js(tmp_path, "probe.js", "const txt = formatPrice(changeAmount);\n")
    rep = cvp.Report()
    assert cvp.check_js_formatting(tmp_path, rep) == 1
    assert [f.rule for f in rep.findings] == ["R3"]
    assert rep.errors


def test_r3_silent_when_sign_is_explicit(tmp_path: Path) -> None:
    _js(
        tmp_path,
        "probe.js",
        'const txt = `${changeAmount >= 0 ? "+" : "-"}${formatPrice(Math.abs(changeAmount))}`;\n',
    )
    rep = cvp.Report()
    assert cvp.check_js_formatting(tmp_path, rep) == 1
    assert rep.findings == []


def test_r3_ignores_formats_without_a_direction_word(tmp_path: Path) -> None:
    """`formatPrice(lastPrice)` 是正数语义，不该要求它自己拼符号。"""
    _js(tmp_path, "probe.js", "const txt = formatPrice(lastPrice);\n")
    rep = cvp.Report()
    assert cvp.check_js_formatting(tmp_path, rep) == 1
    assert rep.findings == []


# ── R4：判 null 不判类型（note 级） ───────────────────────────
NULL_GUARD_PROPERTY_READ = (
    'const div = dc.divergence_pct;\nconst txt = div === null ? "—" : div.toFixed(2);\n'
)


def test_r4_notes_property_read_with_null_guard(tmp_path: Path) -> None:
    _js(tmp_path, "probe.js", NULL_GUARD_PROPERTY_READ)
    rep = cvp.Report()
    cvp.check_js_formatting(tmp_path, rep)
    assert [f.rule for f in rep.findings] == ["R4"]
    assert [f.severity for f in rep.findings] == ["note"], "R4 是 note，不该阻断 CI"
    assert not rep.errors


def test_r4_ignores_value_wrapped_in_num(tmp_path: Path) -> None:
    """`const pct = num(metrics.change_pct)` 只可能是 null 或数字 ⇒ 判 null 已足够。"""
    _js(
        tmp_path,
        "probe.js",
        'const pct = num(metrics.change_pct);\nconst txt = pct === null ? "—" : pct.toFixed(2);\n',
    )
    rep = cvp.Report()
    cvp.check_js_formatting(tmp_path, rep)
    assert rep.findings == []


def test_r4_ignores_function_parameter(tmp_path: Path) -> None:
    """函数形参的契约由调用方给，不是 payload 键读法（`url_safety.js` 的 `url`）。"""
    _js(tmp_path, "probe.js", 'const txt = url === null ? "—" : url.toString();\n')
    rep = cvp.Report()
    cvp.check_js_formatting(tmp_path, rep)
    assert rep.findings == []


def test_r4_allow_table_silences_a_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _js(tmp_path, "probe.js", NULL_GUARD_PROPERTY_READ)
    monkeypatch.setitem(cvp._R4_ALLOW, "dashboard/probe.js:div", "上游必给该键（测试用）")
    rep = cvp.Report()
    cvp.check_js_formatting(tmp_path, rep)
    assert rep.findings == []


def test_cat_filter_runs_only_named_rule(tmp_path: Path) -> None:
    """`--cat R4` 不能顺手把 R3 的 error 也报出来（selftest 夹具靠这个）。"""
    _js(tmp_path, "probe.js", "const txt = formatPrice(changeAmount);\n" + NULL_GUARD_PROPERTY_READ)
    rep = cvp.Report()
    cvp.check_js_formatting(tmp_path, rep, {"R4"})
    assert [f.rule for f in rep.findings] == ["R4"]
    assert not rep.errors


# ── 输出与 CLI ────────────────────────────────────────────────
def test_render_counts_findings_per_rule() -> None:
    rep = cvp.Report()
    rep.add(cvp.Finding("R1", "error", "a.py:1", "ev", "why", "fix"))
    rep.add(cvp.Finding("R3", "error", "b.js:2", "ev", "why", "fix"))
    body = cvp.render(rep, "除法 2 处")
    assert "按判据：R1 × 1 · R3 × 1" in body
    assert "合计 2 个 error、0 个 note" in body


def test_render_is_deterministic() -> None:
    rep = cvp.Report()
    rep.add(cvp.Finding("R1", "error", "a.py:1", "ev", "why", "fix"))
    assert cvp.render(rep, "x") == cvp.render(rep, "x")


def test_render_says_loudly_when_nothing_found() -> None:
    assert "无发现" in cvp.render(cvp.Report(), "x")


def test_default_log_is_not_markdown() -> None:
    """生成物不能叫 `.md`：check_all_claims / check_doc_counts 会 rglob("*.md")，把
    里面的路径与行号当文档断言去验 ⇒ 改一行 dashboard 就让它假阳性。"""
    assert cvp.DEFAULT_LOG.endswith(".log")
    assert not cvp.DEFAULT_LOG.endswith(".md")


def test_main_returns_1_when_an_error_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _py(
        tmp_path,
        "application/probe.py",
        "def ratio(last_price, base):\n    return last_price / base\n",
    )
    monkeypatch.setattr(cvp, "ROOT", tmp_path)
    assert cvp.main(["--no-log", "--deployed", "", "--cat", "R1"]) == 1


def test_main_returns_0_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cvp, "ROOT", tmp_path)
    assert cvp.main(["--no-log", "--deployed", ""]) == 0
    assert "无发现" in capsys.readouterr().out


def test_main_rejects_unknown_cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cvp, "ROOT", tmp_path)
    assert cvp.main(["--no-log", "--deployed", "", "--cat", "R9"]) == 2


def test_main_rejects_exemption_without_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cvp, "ROOT", tmp_path)
    monkeypatch.setitem(cvp._R4_ALLOW, "dashboard/x.js:y", "   ")
    assert cvp.main(["--no-log", "--deployed", ""]) == 1


def test_main_writes_the_log_where_it_says(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cvp, "ROOT", tmp_path)
    log = tmp_path / "docs/audit/probe-findings.log"
    assert cvp.main(["--log", str(log), "--deployed", "", "-q"]) == 0
    assert "无发现" in log.read_text(encoding="utf-8")
