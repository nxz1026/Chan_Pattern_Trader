"""``scripts/factor_backfill.py`` 的测试。

这个脚本是 364 行的运维入口，2026-09-25 之前**不在任何门禁覆盖范围内**（CI 只跑
``ruff check cpt tests`` + ``mypy cpt``），而且没有任何测试 —— 于是它自带的两份
重复实现（``FactorRow`` / ``upsert_factor_rows``）和一份有顺序 bug 的市场推断
（``_code_to_tx``）都没人发现。本文件把它的**纯逻辑**钉住。

网络与 DB 一律注入假实现，绝不真连（踩过：这类脚本"默认联网"会让跑测试写脏生产库）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from cpt.adapters.a_share_factor import factor_source_ref
from cpt.adapters.a_share_public import TENCENT_KLINE_URL

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "factor_backfill.py"


@pytest.fixture(scope="module")
def script() -> Any:
    """按路径加载脚本模块。

    **必须先把模块注册进 ``sys.modules`` 再 ``exec_module``** —— 脚本用了
    ``@dataclass``，dataclass 在 exec 期间会回头查 ``sys.modules[cls.__module__]``，
    只调 ``module_from_spec`` 会拿到 None 并抛
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    """
    name = "factor_backfill_script"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ 市场前缀


def test_fetch_uses_authoritative_market_prefix(script: Any, monkeypatch: Any) -> None:
    """**回归**：``920201`` 必须推成 ``bj920201``，不能是 ``sh920201``。

    脚本原先自带 ``_code_to_tx``，把 ``9``（沪 B）写在 ``92``（北交所新代码段）
    之前 —— ``startswith(("6","9","5"))`` 抢先命中，``92`` 那一支成了死代码。
    同一个顺序 bug 在 ``a_share_public.normalize_code`` 的注释里被点名过
    （``a_share_local._to_wind_code``，R17 修掉），这第三份一直没跟上。
    """
    seen: list[str] = []

    def fake_pair(tx_sym: str, days: int) -> Any:
        seen.append(tx_sym)
        return ([["2000-01-01", "0", "10"]], [["2000-01-01", "0", "12"]])

    monkeypatch.setattr(script, "_fetch_tx_pair", fake_pair)
    script.fetch_tx_factor_rows("920201")
    assert seen == ["bj920201"], "920201 又被推成沪市了"


def test_fetch_handles_bj_prefixes_the_old_helper_rejected(script: Any, monkeypatch: Any) -> None:
    """``83/87/88`` 开头的北交所代码：旧实现直接 ``ValueError``，现在要能拉。"""
    seen: list[str] = []

    def fake_pair(tx_sym: str, days: int) -> Any:
        seen.append(tx_sym)
        return ([["2000-01-01", "0", "10"]], [["2000-01-01", "0", "12"]])

    monkeypatch.setattr(script, "_fetch_tx_pair", fake_pair)
    script.fetch_tx_factor_rows("830799")
    assert seen == ["bj830799"]


def test_script_has_no_local_market_inference(script: Any) -> None:
    """本地市场推断必须已删除（判 AST，不判字符串 —— 注释里还留着历史说明）。"""
    import ast

    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert "_code_to_tx" not in defined


def test_script_reuses_shared_symbols_instead_of_copying(script: Any) -> None:
    """脚本必须**导入**共享符号，不许再自带副本（判 AST，不判字符串）。

    2026-09-25 之前本脚本自带 ``FactorRow`` / ``upsert_factor_rows`` / ``SOURCE_TX``
    / ``TX_ENDPOINT`` 四份副本，与 ``cpt/adapters/a_share_factor.py`` 分叉
    （8 列 vs 9 列、``tencent_fqkline`` vs ``tx:fqkline``）。这条测试把"副本不许再
    长出来"钉死。

    **必须判 AST 不能搜字符串**：脚本注释里故意留着这段历史说明（"合并前本脚本自带
    一份逐行重复实现"），字符串匹配会假红。
    """
    import ast

    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    forbidden = {"FactorRow", "upsert_factor_rows", "SOURCE_TX", "TX_ENDPOINT", "_code_to_tx"}
    copies = forbidden & (defined | assigned)
    assert not copies, f"脚本又长出了共享符号的本地副本：{sorted(copies)}"

    # 反面也要钉：这些符号必须**确实**从 cpt 导入（否则上面的断言靠"什么都没有"也能过）
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    required = {
        "FactorRow",
        "upsert_factor_rows",
        "TENCENT_KLINE_URL",
        "normalize_code",
        # R20：因子计算收口到 build_factor_rows 后，脚本不再自己拼 FactorRow，
        # 于是 factor_source_ref（FactorRow 的默认 source_ref 构造处）也随之内移。
        # 这里断言的是"必须复用共享实现"，不是"必须直接调用每个符号"。
        "build_factor_rows",
    }
    assert required <= imported, f"脚本没有从 cpt 导入：{sorted(required - imported)}"
    assert "factor_source_ref" not in imported, (
        "脚本又开始自己拼 source_ref 了 —— 因子计算必须整条走 build_factor_rows，"
        "否则腾讯 hfq 序列退化（2026-09-30 实测 40 只票 15 只中招）又挡不住"
    )


# ------------------------------------------------------------------ 因子计算


def test_fetch_computes_hfq_over_raw(script: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        script,
        "_fetch_tx_pair",
        lambda tx_sym, days: ([["2000-01-01", "0", "10"]], [["2000-01-01", "0", "12"]]),
    )
    rows = script.fetch_tx_factor_rows("600519")
    assert len(rows) == 1
    assert rows[0].code == "600519"
    assert rows[0].trade_date == "2000-01-01"
    assert rows[0].hfq_factor == pytest.approx(1.2)
    assert rows[0].source_ref == factor_source_ref("2000-01-01")


def test_fetch_keeps_only_overlapping_dates(script: Any, monkeypatch: Any) -> None:
    """只有 raw 或只有 hfq 的交易日必须丢掉 —— 因子必须同源同对。"""
    monkeypatch.setattr(
        script,
        "_fetch_tx_pair",
        lambda tx_sym, days: (
            [["2000-01-01", "0", "10"], ["2000-01-02", "0", "11"]],
            [["2000-01-01", "0", "12"], ["2000-01-03", "0", "13"]],
        ),
    )
    rows = script.fetch_tx_factor_rows("600519")
    assert [r.trade_date for r in rows] == ["2000-01-01"]


def test_fetch_skips_zero_raw_close(script: Any, monkeypatch: Any) -> None:
    """``raw == 0`` 会除零 —— 必须跳过而不是抛异常。"""
    monkeypatch.setattr(
        script,
        "_fetch_tx_pair",
        lambda tx_sym, days: (
            [["2000-01-01", "0", "0"], ["2000-01-02", "0", "11"]],
            [["2000-01-01", "0", "12"], ["2000-01-02", "0", "13"]],
        ),
    )
    rows = script.fetch_tx_factor_rows("600519")
    assert [r.trade_date for r in rows] == ["2000-01-02"]


# ------------------------------------------------------------------ 端点收口


def test_fetch_pair_uses_the_shared_endpoint(script: Any, monkeypatch: Any) -> None:
    """HTTP 请求必须打 ``TENCENT_KLINE_URL``（脚本不再自带端点常量）。

    同时钉住"两次请求"（``bfq`` + ``hfq``）：一次不够，因为因子要 raw 与 hfq 相除。
    """
    seen: list[str] = []

    class _Resp:
        def read(self) -> bytes:
            payload = {
                "data": {
                    "sh600519": {
                        "day": [["2000-01-01", "0", "10"]],
                        "hfqday": [["2000-01-01", "0", "12"]],
                    }
                }
            }
            return json.dumps(payload).encode("gbk")

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
        seen.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(script.urllib.request, "urlopen", fake_urlopen)
    raw, hfq = script._fetch_tx_pair("sh600519", 800)

    assert raw == [["2000-01-01", "0", "10"]]
    assert hfq == [["2000-01-01", "0", "12"]]
    assert len(seen) == 2, "应各请求一次 bfq 与 hfq"
    assert all(url.startswith(f"{TENCENT_KLINE_URL}?param=sh600519,day,,,800,") for url in seen)
    # 第一次是不复权（adj 为空串，URL 以逗号收尾），第二次才是 hfq
    assert seen[0].endswith(",") and not seen[0].endswith("hfq")
    assert seen[1].endswith(",hfq")


# --------------------------------------------------------------------------- #
# Wind 兜底接线（台账决策 C1）
#
# 守门规矩（docs/pending-wiring.md 约束 4）：**入口必须是生产路径**。这里全部从
# ``scripts.factor_backfill.main()`` 出发，只把 DB 与 Wind CLI 这两个**外部边界**
# 换成假的；``_to_wind_code`` 与 ``fetch_adjust_factors`` 跑的是真实现 ——
# "接线接上了没有"必须由这条链路证明，模块级自证式用例不算数。
# --------------------------------------------------------------------------- #


def _install_fake_psycopg(
    monkeypatch: Any, local_factors: dict[str, float] | None = None
) -> list[tuple[str, Any]]:
    """注入够用的假 ``psycopg``（CI 没装、也没库）。

    只实现 ``main()`` 走到的部分：``connect(**kwargs)`` 上下文 + ``cursor()``。
    返回 SQL 日志，供断言"写库时带的 source/source_url 是哪一个源"。

    :param local_factors: 这只票**已落盘**的因子。R31 的 Wind 基准闸要拿它对账；
        默认空 = 本地一行都没有，闸会以 ``wind_basis_unknown`` 拒绝 —— 那也是真实
        行为：没有重叠就无法确认两家的后复权基准是否同一个。
    """
    import types

    logs: list[tuple[str, Any]] = []
    factors = dict(local_factors or {})

    class _Cursor:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def execute(self, sql: str, params: Any = None) -> None:
            logs.append((sql, params))
            flat = " ".join(sql.split()).lower()
            if "from asel.ref_adjust_factor" in flat and "source is not null" in flat:
                self._rows = list(factors.items())
            else:
                # 其余查询（unverifiable_dates 等）返回空 = 没有不可考日期，行全部放行
                self._rows = []

        def fetchall(self) -> list[Any]:
            return self._rows

        def executemany(self, sql: str, payload: Any) -> None:
            logs.append((sql, payload))

    class _Conn:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def cursor(self) -> Any:
            return _Cursor()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    module = types.ModuleType("psycopg")
    module.connect = lambda **kwargs: _Conn()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", module)
    return logs


def _cli_missing_init(missing_cli: Path) -> Any:
    """造一个「CLI 指向不存在文件」的 ``WindSourceClient.__init__`` 替身（R50）。

    **不替换整个类，也不注入假 runner** —— 只把构造参数里的 ``cli_script``
    钉到不存在的路径。这样：

    - ``availability()`` 走的是**真实实现**，按真实顺序判到
      ``if not self._cli_script.is_file(): return False, "wind_cli_missing:..."``；
    - ``call()`` 走的是**真实实现**，按真实逻辑抛 ``WindUnavailableError``；
    - ``_wind_fallback()`` 的 ``except WindUnavailableError`` 分支因此被真实走到。

    换句话说：被钉死的只是**环境前提**，被验证的是**产品行为**。

    ⚠️ 形参名故意**不叫** ``cli_script``：内层关键字形参同名会遮蔽闭包变量，
    ``None`` 分支就回落到 ``DEFAULT_CLI_SCRIPT``（本机真实存在的那个文件），
    availability 照样返回 True，替身静默失效。踩过。
    """
    from cpt.adapters import wind_source as wind_mod

    real_init = wind_mod.WindSourceClient.__init__
    assert not missing_cli.is_file(), f"替身要求 CLI 路径不存在，但 {missing_cli} 在"

    def patched(
        self: Any,
        *,
        cli_script: Path | None = None,
        config_path: Path | None = None,
        ledger_path: Path | None = None,
        node: str | None = None,
        timeout: float = 90.0,
        runner: Any = None,
    ) -> None:
        real_init(
            self,
            cli_script=cli_script if cli_script is not None else missing_cli,
            config_path=config_path,
            ledger_path=ledger_path,
            node=node,
            timeout=timeout,
            runner=runner,
        )

    return patched


def _prepare_main(
    script: Any,
    monkeypatch: Any,
    codes: list[str],
    local_factors: dict[str, float] | None = None,
) -> list[tuple[str, Any]]:
    """把 ``main()`` 的 DB 边界全部假掉（代码清单 + 连接参数 + psycopg）。"""
    monkeypatch.setattr(script, "list_all_a_codes", lambda: list(codes))
    monkeypatch.setattr(script, "connection_kwargs", lambda: {"dbname": "fake"})
    return _install_fake_psycopg(monkeypatch, local_factors)


def test_main_default_path_never_touches_wind(script: Any, monkeypatch: Any) -> None:
    """**默认行为不得改变**：不带 ``--wind-fallback`` 时一个 Wind 调用都不许发。"""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("默认路径不该走到 Wind")

    monkeypatch.setattr(script, "fetch_wind_factor_rows", forbidden)

    def boom(code: str, *, days: int = 0) -> Any:
        raise script.ASharePublicError("腾讯不供该标的后复权")

    monkeypatch.setattr(script, "fetch_tx_factor_rows", boom)
    _prepare_main(script, monkeypatch, ["600519"])

    assert script.main(["--mode", "full", "--sleep-ms", "0", "--dry-run"]) == 0


def test_main_wind_fallback_wires_both_orphan_functions(
    script: Any, monkeypatch: Any, caplog: Any
) -> None:
    """**接线守门**：``main()`` + ``--wind-fallback`` 必须真的调到

    - ``a_share_local.AShareLocalClient._to_wind_code``（裸码 → Wind 代码）
    - ``wind_source.WindSourceClient.fetch_adjust_factors``（取因子）

    两者接线前全仓 0 代码引用；本用例用 spy 包住**真实现**，断言的既是"调到了"，
    也是"传进去的 Wind 代码对"。
    """
    from cpt.adapters import a_share_local as local_mod
    from cpt.adapters import wind_source as wind_mod

    seen: dict[str, Any] = {}
    real_code = local_mod.AShareLocalClient._to_wind_code

    def spy_code(code: str) -> str:
        seen["code_in"] = code
        windcode = real_code(code)
        seen["windcode"] = windcode
        return windcode

    # 基准闸（R31）：本地要已有 3 天以上**同基准**因子，Wind 才被放行。
    # 这里两边都给 2.5，就是"基准一致"的最小可信场景。
    wind_days = {"2026-09-28": 2.5, "2026-09-29": 2.5, "2026-09-30": 2.5}

    def spy_factors(self: Any, windcode: str, **kwargs: Any) -> dict[str, float]:
        seen["fetch_windcode"] = windcode
        seen["fetch_kwargs"] = kwargs
        return dict(wind_days)

    monkeypatch.setattr(local_mod.AShareLocalClient, "_to_wind_code", staticmethod(spy_code))
    monkeypatch.setattr(wind_mod.WindSourceClient, "fetch_adjust_factors", spy_factors)

    def boom(code: str, *, days: int = 0) -> Any:
        raise script.ASharePublicError("腾讯不供该标的后复权")

    monkeypatch.setattr(script, "fetch_tx_factor_rows", boom)
    logs = _prepare_main(script, monkeypatch, ["600519"], dict(wind_days))

    with caplog.at_level("INFO", logger="factor_backfill"):
        assert script.main(["--mode", "full", "--wind-fallback", "--sleep-ms", "0"]) == 0

    assert seen["code_in"] == "600519"
    assert seen["windcode"] == "600519.SH"
    assert seen["fetch_windcode"] == "600519.SH"  # 用转换后的代码去查 Wind
    assert "begin_date" in seen["fetch_kwargs"] and "end_date" in seen["fetch_kwargs"]

    # 写库必须带上 Wind 的 source/source_url，能和腾讯行区分
    inserts = [payload for sql, payload in logs if "INSERT INTO asel.ref_adjust_factor" in sql]
    assert inserts, "Wind 兜底取到行之后必须真的写库"
    rows = {r[1]: r for r in inserts[0]}
    assert set(rows) == set(wind_days), f"三天都该落库：{sorted(rows)}"
    row = rows["2026-09-30"]
    assert (row[0], row[1], row[3], row[4]) == (
        "600519",
        "2026-09-30",
        script.SOURCE_WIND,
        script.WIND_SOURCE_URL,
    )
    assert "Wind 兜底命中 600519" in caplog.text


def test_main_wind_fallback_degrades_when_wind_missing(
    script: Any, monkeypatch: Any, caplog: Any
) -> None:
    """Wind 不可用（CI 的常态：没有 CLI、没有密钥）→ 记原因、继续，返回码仍是 0。

    ⚠️ R50：这条原先**不注入任何 Wind 替身**，直接吃真实环境的
    ``WindSourceClient.availability()``。那等于把「这台机器没装 Wind」
    当成测试前提 —— 而本机三件套齐全（CLI 在、密钥在、node 可解析），
    ``availability()`` 返回 ``(True, "")``，于是走进 R31 的基准闸分支，
    产出 ``wind_basis_unknown`` 而不是本用例要的 ``wind_unavailable``。

    **症状**：同一份代码在 Oracle 上绿、在本机红，CI 换台机器结果就变。
    **根因**：判据依赖环境。⇒ 改成显式制造「不可用」（见 :func:`_cli_missing_init`）。

    仍然**不注入假 runner**：``availability() → call() → WindUnavailableError``
    这条异常链是真实的，被钉死的只有 CLI 路径这一个环境前提。
    """
    from cpt.adapters import wind_source as wind_mod

    monkeypatch.setattr(
        wind_mod.WindSourceClient,
        "__init__",
        _cli_missing_init(Path("/nonexistent/wind-mcp-skill/scripts/cli.mjs")),
        raising=True,
    )

    def boom(code: str, *, days: int = 0) -> Any:
        raise script.ASharePublicError("腾讯不供该标的后复权")

    monkeypatch.setattr(script, "fetch_tx_factor_rows", boom)
    _prepare_main(script, monkeypatch, ["600519"])

    with caplog.at_level("INFO", logger="factor_backfill"):
        rc = script.main(["--mode", "full", "--wind-fallback", "--sleep-ms", "0", "--dry-run"])

    assert rc == 0, "Wind 接不通绝不能把整轮回填搞崩"
    assert "wind_unavailable" in caplog.text
    assert "wind_cli_missing" in caplog.text, "降级理由必须说清是 CLI 缺失，便于运维定位"
    assert "本地源permanent失败" in caplog.text  # 两类原因必须可分


def test_main_wind_fallback_never_probes_wind_when_local_succeeds(
    script: Any, monkeypatch: Any, caplog: Any
) -> None:
    """本地源成功 ⇒ 连 ``availability()`` 都不该探，更不该发 Wind 请求。

    这是上面那条的**镜像守卫**，也是本机（Wind 三件套齐全）唯一能验证
    「兜底判定写对了」的用例。上一条把 CLI 指向不存在的文件来制造「不可用」，
    如果哪天 ``main()`` 的兜底判断写反了 —— 比如无条件探一次 availability、
    或在本地成功时也去问 —— 只有「不可用」那一条测试发现得了。

    ⚠️ 探活不是免费的：真发一次 Wind 调用会**消耗真实额度**（见
    ``wind_source`` 模块 docstring 的配额纪律）。所以这里把 availability
    也 spy 住：连探都不许探。
    """
    from cpt.adapters import wind_source as wind_mod

    probe: list[str] = []
    real_availability = wind_mod.WindSourceClient.availability

    def spy_availability(self: Any) -> tuple[bool, str]:
        probe.append("availability")
        return real_availability(self)

    monkeypatch.setattr(wind_mod.WindSourceClient, "availability", spy_availability)
    monkeypatch.setattr(
        script,
        "fetch_wind_factor_rows",
        lambda code: pytest.fail("本地源已成功，不该调用 Wind 兜底"),
    )
    monkeypatch.setattr(
        script,
        "fetch_tx_factor_rows",
        lambda code, *, days=0: [
            script.FactorRow(code=code, trade_date="2026-09-30", hfq_factor=1.5)
        ],
    )
    _prepare_main(script, monkeypatch, ["600519"])

    with caplog.at_level("INFO", logger="factor_backfill"):
        assert script.main(["--mode", "full", "--wind-fallback", "--sleep-ms", "0"]) == 0

    assert probe == [], f"本地源成功时连 availability 都不该探（会消耗真实额度）：{probe}"
    assert "Wind 兜底" not in caplog.text, f"本地源已成功却打了 Wind 兜底日志：\n{caplog.text}"
    assert "新增 1 行" in caplog.text


def test_main_wind_fallback_degrades_under_real_availability(
    script: Any, monkeypatch: Any, caplog: Any
) -> None:
    """**真实 availability()** 下，兜底失败必须降级不崩，且理由落在 ``wind_*`` 族。

    这条刻意**不**改 availability，让它按真实环境判定：CI 上返回不可用、
    本机返回可用。两种环境下 ``rc`` 都必须是 0，日志都必须说清是哪一档
    （``wind_unavailable`` / ``wind_error`` / ``wind_quota`` / ``wind_empty`` /
    ``wind_basis_*``）—— 这样「环境变了」不再等于「测试红了」。

    本地一行因子都没有 ⇒ 基准闸必然拒绝（overlap=0 < ``WIND_BASIS_MIN_OVERLAP``），
    所以不必真发 Wind 请求就能走到「取到了但不可信」这一档，**不消耗任何额度**。
    """
    from cpt.adapters import wind_source as wind_mod

    def boom(code: str, *, days: int = 0) -> Any:
        raise script.ASharePublicError("腾讯不供该标的后复权")

    monkeypatch.setattr(script, "fetch_tx_factor_rows", boom)
    _prepare_main(script, monkeypatch, ["600519"])

    with caplog.at_level("INFO", logger="factor_backfill"):
        rc = script.main(["--mode", "full", "--wind-fallback", "--sleep-ms", "0", "--dry-run"])

    assert rc == 0, "Wind 兜底任何一档失败都不能把整轮回填搞崩"
    assert "wind_" in caplog.text, f"降级理由必须带 wind_ 前缀以便运维分流：\n{caplog.text[-400:]}"
    assert "本地源permanent失败" in caplog.text
    # 真值写进断言信息：具体哪一档取决于环境，测试不绑它
    ok, reason = wind_mod.WindSourceClient().availability()
    assert ok or reason, "availability() 必须给出可解释的真值"


def test_main_wind_fallback_keeps_local_rows_untouched(
    script: Any, monkeypatch: Any, caplog: Any
) -> None:
    """本地成功时不该走 Wind（兜底只在"本地取不到"时触发）。"""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("本地成功时不该调 Wind")

    monkeypatch.setattr(script, "fetch_wind_factor_rows", forbidden)
    monkeypatch.setattr(
        script,
        "fetch_tx_factor_rows",
        lambda code, *, days=0: [
            script.FactorRow(code=code, trade_date="2026-09-30", hfq_factor=1.5)
        ],
    )
    logs = _prepare_main(script, monkeypatch, ["600519"])

    with caplog.at_level("INFO", logger="factor_backfill"):
        assert script.main(["--mode", "full", "--wind-fallback", "--sleep-ms", "0"]) == 0

    inserts = [payload for sql, payload in logs if "INSERT INTO asel.ref_adjust_factor" in sql]
    assert inserts and inserts[0][0][4] == TENCENT_KLINE_URL  # 仍标腾讯源


def test_main_wind_fallback_reports_invalid_code(
    script: Any, monkeypatch: Any, caplog: Any
) -> None:
    """非法代码 → ``wind_code_invalid``，且**不许**伪造一个 Wind 调用。

    这条走的是真 ``fetch_wind_factor_rows``（``_to_wind_code`` 在转码阶段就抛），
    所以同时钉住"非法输入不会静默产出错误代码"这条契约。
    """
    from cpt.adapters import wind_source as wind_mod

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("非法代码不该发出任何 Wind 调用")

    monkeypatch.setattr(wind_mod.WindSourceClient, "fetch_adjust_factors", forbidden)

    def boom(code: str, *, days: int = 0) -> Any:
        raise script.ASharePublicError("腾讯不认识")

    monkeypatch.setattr(script, "fetch_tx_factor_rows", boom)
    _prepare_main(script, monkeypatch, ["700000"])

    with caplog.at_level("INFO", logger="factor_backfill"):
        rc = script.main(["--mode", "full", "--wind-fallback", "--sleep-ms", "0", "--dry-run"])

    assert rc == 0
    assert "wind_code_invalid" in caplog.text


# --------------------------------------------------------------------------- #
# Wind 基准闸（R31）：两家的后复权基准不是同一个时**不许写**
# --------------------------------------------------------------------------- #


def _wind_rows(script: Any, factors: dict[str, float]) -> list[Any]:
    return [
        script.FactorRow(code="600519", trade_date=day, hfq_factor=value, source=script.SOURCE_WIND)
        for day, value in sorted(factors.items())
    ]


def test_check_wind_basis_accepts_matching_basis(script: Any) -> None:
    days = {"2026-09-28": 2.5, "2026-09-29": 2.5, "2026-09-30": 2.5}
    assert script.check_wind_basis(_wind_rows(script, days), dict(days)) == ""


def test_check_wind_basis_rejects_measured_22pct_gap(script: Any) -> None:
    """R31 真机：Wind 因子 8.6469 vs 本地 7.0605 = +22.47% → 必须拒绝。

    这不是精度问题：落库是 ``ON CONFLICT DO UPDATE`` 覆盖式 upsert，写进去等于
    把主源那段历史换一套基准，并在交界处造出一个 22% 的假跳空。
    """
    local = {"2026-09-28": 7.06, "2026-09-29": 7.06, "2026-09-30": 7.0605}
    wind = {d: v * 1.2247 for d, v in local.items()}
    note = script.check_wind_basis(_wind_rows(script, wind), local)
    assert note.startswith("wind_basis_mismatch")
    assert "22.47%" in note.replace("+", "").replace(" ", "") or "22.4" in note


def test_check_wind_basis_refuses_when_overlap_too_small(script: Any) -> None:
    """重叠不足 = **不知道**，不是"大概一致" —— 也要拒绝。"""
    local = {"2026-09-30": 7.06}
    wind = {"2026-09-28": 2.0, "2026-09-29": 2.0, "2026-09-30": 2.0}
    assert script.check_wind_basis(_wind_rows(script, wind), local).startswith("wind_basis_unknown")
    assert script.check_wind_basis(_wind_rows(script, wind), {}).startswith("wind_basis_unknown")


def test_main_wind_fallback_does_not_write_on_basis_mismatch(
    script: Any, monkeypatch: Any, caplog: Any
) -> None:
    """端到端守住：基准对不上时**一行都不许落库**，且要报出偏差数字。"""
    local = {"2026-09-28": 7.06, "2026-09-29": 7.06, "2026-09-30": 7.0605}
    monkeypatch.setattr(
        script,
        "fetch_wind_factor_rows",
        lambda code, **kwargs: _wind_rows(script, {d: v * 1.2247 for d, v in local.items()}),
    )

    def boom(code: str, *, days: int = 0) -> Any:
        raise script.ASharePublicError("腾讯不供该标的后复权")

    monkeypatch.setattr(script, "fetch_tx_factor_rows", boom)
    logs = _prepare_main(script, monkeypatch, ["600519"], dict(local))

    with caplog.at_level("INFO", logger="factor_backfill"):
        assert script.main(["--mode", "full", "--wind-fallback", "--sleep-ms", "0"]) == 0

    assert not [1 for sql, _ in logs if "INSERT INTO asel.ref_adjust_factor" in sql], (
        "基准对不上还落库 = 静默改写主源历史"
    )
    assert "wind_basis_mismatch" in caplog.text
