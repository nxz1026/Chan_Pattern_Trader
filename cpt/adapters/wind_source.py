"""Wind 万得主通道适配器（R17，A 股）。

## 通道形态（实测，不是猜的）

本机**没有 WindPy**。Wind 的取数路径是 wind-mcp-skill 的本地 CLI：

```
cd /home/ubuntu/.agents/skills/wind-mcp-skill
node scripts/cli.mjs call <server_type> <tool_name> '<params_json>'
```

- 成功：stdout 是数据对象，后端结果在 ``content[0].text``（通常是 JSON 字符串），
  另附 ``cli_meta``；
- 失败：stdout 是 ``{"ok": false, "code": "...", "message": "..."}``，其中
  ``code`` 取 ``AUTH_ERROR`` / ``RATE_LIMIT_ERROR`` / ``PARAM_VALIDATION_ERROR`` /
  ``backend_error`` 等（见 SKILL.md §3）。
- 密钥在 ``~/.wind-aifinmarket/config`` 的 ``WIND_API_KEY``。

## 复权口径（关键，且 R31 实测改过一次结论）

``get_stock_kline`` 的 ``aftype``：**0=前复权，1=后复权，2=不复权**。CPT 本地库
（``public.daily_bar × asel.ref_adjust_factor``）用的是**后复权**，所以本适配器
默认 ``aftype="1"``。同时：**该工具不返回复权因子**，只有价格 —— 想补
``ref_adjust_factor`` 必须同区间调两次（``aftype=2`` 与 ``aftype=1``）再相除，
即 ``factor = hfq_price / raw_price``。

### ⚠️ 「都是后复权」不等于「同一条序列」（R31 真机实测）

600519 / 2026-09-30：

| | 不复权收盘 | 后复权因子 | 后复权收盘 |
|---|---|---|---|
| Wind | 1258.62 | 8.6469 | 10883.14 |
| 腾讯 / 本地库 | 1258.62 | 7.0605 | 8886.536 |

**不复权两边一模一样**（说明底层数据没问题），**因子差 22.47%** —— 差的是
"后复权的基准"，各家自选基准日，跨家不可比。所以：

- 本模块返回的因子是 **Wind 基准**的，不能直接当本地因子用；
- 写进 ``asel.ref_adjust_factor`` 是 ``ON CONFLICT DO UPDATE`` **覆盖式**的，
  而后复权价 = 不复权价 × 因子 ⇒ 写错基准不是"精度差一点"，是**主源历史被改写**
  且在交界处出现假跳空；
- 落地前的基准闸在 ``scripts/factor_backfill.py::check_wind_basis``（R31 加），
  对不齐就带着偏差数字拒绝写入。

## 配额纪律

Wind 调用**消耗真实额度**。所以：
- 本模块的导入与构造**不发请求**；
- :meth:`WindSourceClient.probe` 会真发一次最小调用（这就是"花一次配额探活"），
  因此 ``source_registry`` 默认把它标成 ``skipped``，只有显式 ``include_quota=True`` 才执行；
- 每次调用追加一行到配额台账（JSONL），便于事后核对消耗。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from cpt.domain.models import CanonicalBar

__all__ = [
    "DEFAULT_CLI_SCRIPT",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_LEDGER_PATH",
    "DEFAULT_NODE",
    "DAILY_INTERVAL_MS",
    "WindQuotaError",
    "WindSourceClient",
    "WindSourceError",
    "WindUnavailableError",
]

DEFAULT_CLI_SCRIPT: Final[Path] = Path("/home/ubuntu/.agents/skills/wind-mcp-skill/scripts/cli.mjs")
DEFAULT_CONFIG_PATH: Final[Path] = Path("~/.wind-aifinmarket/config").expanduser()
DEFAULT_LEDGER_PATH: Final[Path] = Path(
    os.getenv("CPT_WIND_LEDGER", "~/.cache/cpt/wind_quota.jsonl")
).expanduser()
#: 跑 ``cli.mjs`` 的 node。**R31：默认的 "node" 在这台机器上根本不在 PATH 里**
#: （node 装在 nvm 下 ``~/.nvm/versions/node/v22.23.2/bin/node``，而 systemd 给
#: 服务的 PATH 是 ``/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin``）。
#: 所以线上要用 ``CPT_WIND_NODE`` 指到绝对路径 —— 拼 PATH 不可靠，nvm 会换版本。
DEFAULT_NODE: Final[str] = os.getenv("CPT_WIND_NODE", "node")
DEFAULT_TIMEOUT_SECONDS: Final[float] = 90.0
DAILY_INTERVAL_MS: Final[int] = 86_400_000
#: 后复权（与 CPT 本地库口径一致）。0=前复权 1=后复权 2=不复权。
AFTYPE_HFQ: Final[str] = "1"
AFTYPE_RAW: Final[str] = "2"

#: Wind 日 K 的 ``TIME`` 前导日期：``2010-01-04T00:00:00.000+02:00`` / ``20260924`` /
#: ``2026/09/24``。只取日期，不带时间与时区偏移（见 :func:`_wind_time_to_ms`）。
_DATE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})")

#: 额度类错误标记（CLI 的 code + message 里可能出现的中文提示）。
_QUOTA_CODES: Final[frozenset[str]] = frozenset({"RATE_LIMIT_ERROR"})
#: 额度类提示语。实测 Wind 的"试用已到期"不带任何英文关键词，必须收录，
#: 否则额度耗尽会被当成普通后端错误，运维会去查网络而不是去充值。
_QUOTA_MARKERS: Final[tuple[str, ...]] = (
    "额度",
    "积分",
    "配额",
    "余额不足",
    "试用已",
    "到期",
    "insufficient",
    "quota",
)


class WindUnavailableError(RuntimeError):
    """Wind 通道不可用（CLI 脚本或 API Key 缺失）。"""


class WindQuotaError(RuntimeError):
    """Wind 额度不足/被限流 —— 与"数据源坏了"是两回事，必须分开报。"""


class WindSourceError(RuntimeError):
    """其他 Wind 错误（参数、后端、网络、解析）。"""


@dataclass(frozen=True)
class WindCall:
    """一次 CLI 调用的回执（便于测试与台账）。"""

    server_type: str
    tool_name: str
    params: Mapping[str, Any]
    ok: bool
    code: str
    message: str
    data: Mapping[str, Any]


Runner = Callable[[Sequence[str], Path, float], "subprocess.CompletedProcess[str]"]


def _default_runner(
    argv: Sequence[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv 是固定结构，参数以 JSON 单参传入
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _read_api_key(config_path: Path) -> str | None:
    """从 ``WIND_API_KEY=xxx`` 形式的配置文件里读密钥（不打印、不外传）。"""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "WIND_API_KEY" and value.strip():
            return value.strip()
    return None


def _to_date_str(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class WindSourceClient:
    """Wind CLI 客户端（窄接口：K 线 / 复权因子 / 探活）。

    Args:
        cli_script: ``cli.mjs`` 路径。
        config_path: 含 ``WIND_API_KEY`` 的配置文件。
        ledger_path: 配额台账（JSONL）；``None`` 关闭记账。
        node: 跑 CLI 的 node（裸名走 PATH 查找，也可给绝对路径）。
        timeout: 单次调用超时（秒）。
        runner: 注入式执行器，测试用。
    """

    def __init__(
        self,
        *,
        cli_script: Path | None = None,
        config_path: Path | None = None,
        ledger_path: Path | None = None,
        node: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        runner: Runner | None = None,
    ) -> None:
        self._cli_script = Path(cli_script) if cli_script is not None else DEFAULT_CLI_SCRIPT
        self._config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
        self._ledger_path = Path(ledger_path) if ledger_path is not None else DEFAULT_LEDGER_PATH
        self._node = node if node is not None else DEFAULT_NODE
        self._timeout = timeout
        self._runner: Runner = runner if runner is not None else _default_runner
        self._calls = 0

    @property
    def call_count(self) -> int:
        """本实例已发起的调用次数（含失败）—— 测试与配额核对用。"""
        return self._calls

    def _resolve_node(self) -> str | None:
        """把 :attr:`_node` 解析成可执行路径；解析不到返回 ``None``。

        给的是裸名（无目录分隔符）就当命令名查 PATH；给了路径就当路径，
        **不要求可执行**（测试注入假 node 文件时才能过）。
        """
        if Path(self._node).name == self._node:
            return shutil.which(self._node)
        return self._node if Path(self._node).is_file() else None

    def availability(self) -> tuple[bool, str]:
        """``(是否可用, 原因)``；不联网。"""
        if not self._cli_script.is_file():
            return False, f"wind_cli_missing:{self._cli_script}"
        if _read_api_key(self._config_path) is None:
            return False, f"wind_api_key_missing:{self._config_path}"
        # R31：这一条以前**不存在**，于是 availability() 在「CLI 文件在、密钥在，
        # 但 node 不在 PATH」时返回 (True, "") —— 一个可证伪的假承诺。真机后果：
        # 线上服务（systemd PATH 无 node）每次 Wind 调用都死在 spawn 上，而体检
        # 报「可用」。**要判断能不能跑，就得把真正要执行的东西也查一遍。**
        if self._resolve_node() is None:
            return False, f"wind_node_missing:{self._node}"
        return True, ""

    def call(
        self,
        server_type: str,
        tool_name: str,
        params: Mapping[str, Any],
        *,
        record: bool = True,
    ) -> WindCall:
        """执行一次 CLI 调用。

        Raises:
            WindUnavailableError: CLI 或密钥缺失。
            WindQuotaError: 额度/限流。
            WindSourceError: 其他失败（含超时、stdout 非 JSON、``ok:false``）。
        """
        available, reason = self.availability()
        if not available:
            raise WindUnavailableError(reason)

        argv = [
            self._resolve_node() or self._node,
            str(self._cli_script),
            "call",
            server_type,
            tool_name,
            json.dumps(params, ensure_ascii=False),
        ]
        self._calls += 1
        started = time.time()
        try:
            completed = self._runner(argv, self._cli_script.parent, self._timeout)
        except subprocess.TimeoutExpired as exc:
            # ⚠️ ``record=False`` 的调用（如纯探活/手工核对）**不许**记额度账 ——
            # 原先这两条异常路径无条件记账，于是账本里混着「没打算计的调用」，
            # 额度用完的时间点与真实用量对不上（``record`` 参数在这两处被忽略）。
            if record:
                self._record(
                    server_type, tool_name, params, ok=False, code="TIMEOUT", started=started
                )
            raise WindSourceError(
                f"Wind 调用超时（{self._timeout}s）：{server_type}.{tool_name}"
            ) from exc
        except OSError as exc:  # node 不存在等
            if record:
                self._record(
                    server_type, tool_name, params, ok=False, code="SPAWN_ERROR", started=started
                )
            raise WindUnavailableError(f"无法执行 node：{exc}") from exc

        stdout = completed.stdout or ""
        payload = self._parse_stdout(stdout, server_type=server_type, tool_name=tool_name)
        # 两种失败信封都实测存在：工具层的 ``{"ok": false, "code": ...}`` 与
        # MCP 外壳的 ``isError: true``（此时错误文本在 content[0].text 里）。
        ok = payload.get("ok", True) is not False and payload.get("isError") is not True
        code = str(payload.get("code") or ("OK" if ok else "UNKNOWN"))
        message = str(payload.get("message") or "")
        if not ok and not message:
            message = self._extract_error_text(payload)
        if record:
            self._record(server_type, tool_name, params, ok=ok, code=code, started=started)

        if not ok:
            if code in _QUOTA_CODES or any(marker in message for marker in _QUOTA_MARKERS):
                raise WindQuotaError(f"Wind 额度/限流（{code}）：{message}")
            if code == "AUTH_ERROR":
                raise WindUnavailableError(f"Wind 鉴权失败：{message}")
            raise WindSourceError(f"Wind 调用失败（{code}）：{message}")

        return WindCall(
            server_type=server_type,
            tool_name=tool_name,
            params=dict(params),
            ok=True,
            code=code,
            message=message,
            data=self._extract_data(payload),
        )

    @staticmethod
    def _parse_stdout(stdout: str, *, server_type: str, tool_name: str) -> Mapping[str, Any]:
        text = stdout.strip()
        if not text:
            raise WindSourceError(f"Wind 无输出：{server_type}.{tool_name}")
        # CLI 有时会在 JSON 前后打印提示行，取第一个 `{` 到最后一个 `}`。
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise WindSourceError(f"Wind 输出不是 JSON：{text[:200]!r}")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise WindSourceError(f"Wind 输出 JSON 解析失败：{text[:200]!r}") from exc
        if not isinstance(payload, Mapping):
            raise WindSourceError("Wind 输出 JSON 顶层不是对象")
        return payload

    @staticmethod
    def _extract_error_text(payload: Mapping[str, Any]) -> str:
        """从 ``isError: true`` 外壳的 ``content[0].text`` 里取错误文本。"""
        content = payload.get("content")
        if isinstance(content, Sequence) and content and isinstance(content[0], Mapping):
            text = content[0].get("text")
            if isinstance(text, str):
                return text.strip()[:500]
        return ""

    @staticmethod
    def _extract_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """从 CLI 回执里取后端数据：优先解析 ``content[0].text``。"""
        content = payload.get("content")
        if isinstance(content, Sequence) and content and isinstance(content[0], Mapping):
            text = content[0].get("text")
            if isinstance(text, str) and text.strip():
                stripped = text.strip()
                start = stripped.find("{")
                end = stripped.rfind("}")
                if start >= 0 and end > start:
                    try:
                        parsed = json.loads(stripped[start : end + 1])
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, Mapping):
                        return parsed
                return {"text": stripped}
        return dict(payload)

    def _record(
        self,
        server_type: str,
        tool_name: str,
        params: Mapping[str, Any],
        *,
        ok: bool,
        code: str,
        started: float,
    ) -> None:
        if self._ledger_path is None:
            return
        entry = {
            "ts": datetime.now(tz=UTC).isoformat(),
            # 进程号：台账是**共享**的追加文件，没有 pid 就无法归属"这条是谁花的额度"。
            # 实测台账里出现过 13 条空参数 + duration_ms=0.0 的 TIMEOUT 记录，本模块的
            # 超时路径只可能记出 ~timeout 毫秒，说明另有写入方 —— 加 pid 便于追溯。
            "pid": os.getpid(),
            "server_type": server_type,
            "tool_name": tool_name,
            "params_digest": hashlib.sha256(
                json.dumps(params, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:16],
            "ok": ok,
            "code": code,
            "duration_ms": round((time.time() - started) * 1000, 1),
        }
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self._ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:  # 记账失败不能影响取数
            pass

    # ------------------------------------------------------------------ 取数

    def fetch_daily_bars(
        self,
        windcode: str,
        *,
        begin_date: str | date,
        end_date: str | date,
        count: int = 0,
        aftype: str = AFTYPE_HFQ,
    ) -> tuple[CanonicalBar, ...]:
        """拉取后复权日 K（``get_stock_kline``，``period="1d"``）。"""
        call = self.call(
            "stock_data",
            "get_stock_kline",
            {
                "windcode": windcode,
                "begin_date": _to_date_str(begin_date),
                "end_date": _to_date_str(end_date),
                "period": "1d",
                "count": count,
                "aftype": aftype,
            },
        )
        return parse_wind_kline(call.data, windcode=windcode)

    def fetch_corporate_actions(self, windcode: str) -> tuple[CorporateAction, ...]:
        """取该标的的**公司行动**（分红派息 / 送转），按 :class:`CorporateAction` 归一化。

        R37 新增。这是重算后复权因子的**真值来源**：因子可以由「除权除息日 +
        每股派息 + 送转比例」**按定义**算出来，而不必去拟合某个供应商那条带漂移的
        后复权序列（实测漂移可达 6%，见 ``docs/progress-log.md`` R36）。

        ## 为什么解析要**按列名**而不是按下标

        Wind 返回的列**按标的而变**（实测 2026-10-02）：

        - ``000001.SZ`` → ``历史分红除权除息日`` / ``历史分红税前每股派息`` / …
        - ``600036.SH`` → ``历史分红税后每股派息`` / ``内地股票分红红股上市日`` /
          ``历史分红转增比…``（**税后**，还多一个转增列）

        所以按下标取值必然在某些标的上崩（实测：``list.index('历史分红除权除息日')``
        直接 ValueError）。这里一律**按列名子串匹配**，缺哪项就留 ``None``，
        由 :class:`CorporateAction` 显式表达"这项没有"，而不是猜。

        :returns: 按除权日升序的公司行动；**没有记录时返回空元组**（不抛）。
        """
        call = self.call(
            "stock_data",
            "get_stock_events",
            {"question": f"查询{windcode} 的分红派息历史，包括除权除息日、每股派息与送转比例"},
        )
        return parse_corporate_actions(call.data)

    def fetch_adjust_factors(
        self,
        windcode: str,
        *,
        begin_date: str | date,
        end_date: str | date,
        count: int = 0,
    ) -> dict[str, float]:
        """算出 ``{日期: 后复权因子}`` —— **两次调用**（不复权 + 后复权）相除。

        Wind 的 K 线工具不返回因子，只返回价格，所以补 ``ref_adjust_factor``
        的代价是每只标的 2 次调用。批量 backfill 前务必先确认额度（见总计划 §9）。
        """
        raw = parse_wind_kline(
            self.call(
                "stock_data",
                "get_stock_kline",
                {
                    "windcode": windcode,
                    "begin_date": _to_date_str(begin_date),
                    "end_date": _to_date_str(end_date),
                    "period": "1d",
                    "count": count,
                    "aftype": AFTYPE_RAW,
                },
            ).data,
            windcode=windcode,
        )
        hfq = parse_wind_kline(
            self.call(
                "stock_data",
                "get_stock_kline",
                {
                    "windcode": windcode,
                    "begin_date": _to_date_str(begin_date),
                    "end_date": _to_date_str(end_date),
                    "period": "1d",
                    "count": count,
                    "aftype": AFTYPE_HFQ,
                },
            ).data,
            windcode=windcode,
        )
        hfq_by_time = {bar.open_time: bar.close for bar in hfq}
        factors: dict[str, float] = {}
        for bar in raw:
            reference = hfq_by_time.get(bar.open_time)
            if reference is None or bar.close == 0:
                continue
            day = datetime.fromtimestamp(bar.open_time / 1000, tz=UTC).date().isoformat()
            factors[day] = reference / bar.close
        return factors

    def probe(self) -> Mapping[str, Any]:
        """一次最小调用探活 —— **会消耗 1 次 Wind 额度**。

        用 ``get_stock_price_indicators``（单标的、单字段），比 K 线更轻。
        """
        call = self.call("stock_data", "get_stock_price_indicators", {"windcode": "600519.SH"})
        keys = sorted(call.data)[:8]
        return {
            "status": "ok",
            "tool": "get_stock_price_indicators",
            "fields": keys,
            "calls": self._calls,
        }


def _first(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
        upper = name.upper()
        if upper in mapping:
            return mapping[upper]
        lower = name.lower()
        if lower in mapping:
            return mapping[lower]
    return None


@dataclass(frozen=True)
class CorporateAction:
    """一次公司行动（除权除息）。

    :param ex_date: 除权除息日（ISO）。**取不到时该条会被丢弃** —— 没有日期就无法
        定位因子台阶，宁缺勿猜。
    :param cash_pre_tax: 税前每股派息（元）。``None`` = 该标的这次没这一项。
    :param cash_after_tax: 税后每股派息（元）。后复权序列按**实际到手**还是**税前**
        口径，各供应商不同（实测腾讯与 Wind 都在税前附近），所以两个都留着。
    :param share_bonus: 送股比例（每股送几股）。
    :param transfer: 转增比例（每股转增几股）。
    """

    ex_date: str
    cash_pre_tax: float | None = None
    cash_after_tax: float | None = None
    share_bonus: float | None = None
    transfer: float | None = None

    @property
    def share_ratio(self) -> float:
        """送股 + 转增的合计比例（``1 + s`` 里的那个 ``s``）。"""
        return (self.share_bonus or 0.0) + (self.transfer or 0.0)


def _pick_column(columns: list[str], *needles: str) -> int | None:
    """按列名子串找列下标；找不到返回 ``None``（不猜、不抛）。"""
    for idx, name in enumerate(columns):
        if all(n in name for n in needles):
            return idx
    return None


def _to_float_or_none(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_table(node: Any, depth: int = 0) -> Mapping[str, Any] | None:
    """递归找出第一个 ``{"columns": [...], "rows": [...]}`` 表。

    ## 为什么不能假设层级

    ``WindSourceClient.call`` 返回的 ``data`` 是**已解析的 content[0].text**，而
    Wind 的信封本身又套了一层：实测 2026-10-02 的真实形状是::

        data = {"data": {"data": [{"columns": [...], "rows": [...]}], "error": None},
                "error": None}

    第一版按 ``data["data"]`` 取，拿到的是 **dict** 不是 list，于是整表被当成
    「无记录」—— 表现为「Wind 无公司行动记录」，一个**看起来像数据缺失的错误**。
    递归找表比数层级稳：信封套几层、以后再加一层，都不影响。
    """
    if depth > 6:
        return None
    if isinstance(node, Mapping):
        if isinstance(node.get("columns"), list) and isinstance(node.get("rows"), list):
            return node
        for value in node.values():
            found = _find_table(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_table(item, depth + 1)
            if found is not None:
                return found
    return None


def parse_corporate_actions(data: Mapping[str, Any]) -> tuple[CorporateAction, ...]:
    """把 ``get_stock_events`` 的返回体归一化成 :class:`CorporateAction` 序列。

    Wind 的返回是「信封套信封套表」，且**列集合按标的、甚至按次而变**（实测：
    000001 的两次调用分别少了/多了「税后每股派息」「转增比例」），所以定位表用
    递归、取值用**列名子串**，全程不按下标。
    """
    table = _find_table(data)
    if table is None:
        return ()
    columns = [str(c.get("name", "")) for c in (table.get("columns") or [])]
    rows = table.get("rows") or []
    if not columns or not rows:
        return ()

    # ⚠️ R45 修：原来写成 `A or B` —— `_pick_column` 返回**列索引**，
    # 而日期列常常正好是**第 0 列**，`0 or B` 在 Python 里是 `B`
    # ⇒ **索引 0 被当成「没找到」**。
    #
    # 后果不是报错，是**静默返回空元组**（i_date 退化成 None ⇒ 早退），
    # 上层报「Wind 无公司行动记录」—— 一个**看起来像数据缺失**的错误，
    # 而真实数据就在那儿。
    #
    # 为什么难发现：同一份 docstring 明写「**列集合按标的、甚至按次而变**」，
    # 所以「日期列是不是第 0 个」本身**随输入而变** —— 有的标的正常，
    # 有的标的静默返回空，**取决于 Wind 那次返回的列顺序**。
    #
    # ⇒ 必须显式判 `is None`，不能用真值判断。
    i_date = _pick_column(columns, "除权除息日")
    if i_date is None:
        i_date = _pick_column(columns, "分红红股上市日")
    if i_date is None:
        i_date = next((i for i, n in enumerate(columns) if "分红" in n and "日" in n), None)
    if i_date is None:
        return ()
    i_pre = _pick_column(columns, "税前", "派息")
    i_post = _pick_column(columns, "税后", "派息")
    i_bonus = _pick_column(columns, "送股")
    i_transfer = _pick_column(columns, "转增")

    out: list[CorporateAction] = []
    for row in rows:
        if not isinstance(row, list) or i_date >= len(row):
            continue
        raw_date = str(row[i_date] or "").strip()
        if len(raw_date) < 10:  # "2026-09-24" / "2026/09/24"
            continue

        def cell(idx: int | None, _row: list[Any] = row) -> Any:
            return _row[idx] if idx is not None and idx < len(_row) else None

        out.append(
            CorporateAction(
                ex_date=raw_date[:10].replace("/", "-"),
                cash_pre_tax=_to_float_or_none(cell(i_pre)),
                cash_after_tax=_to_float_or_none(cell(i_post)),
                share_bonus=_to_float_or_none(cell(i_bonus)),
                transfer=_to_float_or_none(cell(i_transfer)),
            )
        )
    out.sort(key=lambda a: a.ex_date)
    return tuple(out)


def parse_wind_kline(data: Mapping[str, Any], *, windcode: str) -> tuple[CanonicalBar, ...]:
    """解析 Wind K 线返回体。

    实测列名：``TIME / OPEN / MATCH / HIGH / LOW / TURNOVER / VOLUME /
    CHANGEHANDRATE / AVPRICE``（``MATCH`` 是收盘价）。列名大小写与嵌套层级
    （``data`` / ``rows`` / ``items``）都可能变，所以这里做**宽松定位**：
    先找列表，再按别名取列，找不到就报错而不是静默补 0。
    """
    rows: Any = data
    # 真实形状（实测回执）：``{"data": {"columns": [{"name": "TIME", ...}, ...],
    # "rows": [[...], ...], "unit": {...}}}`` —— 列语义只在 ``columns[].name`` 里，
    # ``rows`` 是**按位置**的数组，所以必须按列名建索引。
    #
    # 表格检测必须在**解包循环内部**做：真形状的内层同时有 ``rows`` 键，若先按
    # 通用键名解包，``columns`` 会被丢掉，于是位置数组被当成
    # ``[TIME, OPEN, MATCH, HIGH, LOW, ...]`` 硬读 —— 实测列序第 6 列是
    # ``TURNOVER``（成交额）而不是 ``VOLUME``（成交量），会**静默**把成交额写成成交量。
    for key in ("data", "rows", "items", "klines", "result"):
        if isinstance(rows, Mapping) and "columns" in rows and "rows" in rows:
            rows = _table_rows_to_dicts(rows, windcode=windcode)
            break
        if isinstance(rows, Mapping) and key in rows:
            rows = rows[key]
    if isinstance(rows, Mapping) and "columns" in rows and "rows" in rows:
        rows = _table_rows_to_dicts(rows, windcode=windcode)
    if isinstance(rows, Mapping):
        # 形如 {"TIME": [...], "OPEN": [...]} 的列式结构
        times = _first(rows, ("TIME", "time", "date"))
        if isinstance(times, Sequence) and times:
            columns = {
                name: _first(rows, aliases)
                for name, aliases in (
                    ("open", ("OPEN", "open")),
                    ("close", ("MATCH", "CLOSE", "close")),
                    ("high", ("HIGH", "high")),
                    ("low", ("LOW", "low")),
                    ("volume", ("VOLUME", "volume")),
                )
            }
            if all(isinstance(value, Sequence) for value in columns.values()):
                rows = [
                    [
                        times[index],
                        columns["open"][index],
                        columns["close"][index],
                        columns["high"][index],
                        columns["low"][index],
                        columns["volume"][index],
                    ]
                    for index in range(len(times))
                ]
    if not isinstance(rows, Sequence) or not rows:
        raise WindSourceError(f"Wind K 线返回体里找不到行数据：{sorted(data)[:8]}")

    bars: list[CanonicalBar] = []
    for row in rows:
        if isinstance(row, Mapping):
            values = [
                _first(row, ("TIME", "time", "date")),
                _first(row, ("OPEN", "open")),
                _first(row, ("MATCH", "CLOSE", "close")),
                _first(row, ("HIGH", "high")),
                _first(row, ("LOW", "low")),
                _first(row, ("VOLUME", "volume")),
            ]
        elif isinstance(row, Sequence) and len(row) >= 6:
            values = list(row[:6])
        else:
            raise WindSourceError(f"Wind K 线行结构不符：{row!r}")
        try:
            open_ms = _wind_time_to_ms(str(values[0]))
            open_price = float(str(values[1]))
            close_price = float(str(values[2]))
            high_price = float(str(values[3]))
            low_price = float(str(values[4]))
            volume = float(str(values[5] or 0))
        except (TypeError, ValueError) as exc:
            raise WindSourceError(f"Wind K 线字段不是数字：{values!r}") from exc
        bars.append(
            CanonicalBar(
                open_time=open_ms,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                close_time=open_ms + DAILY_INTERVAL_MS - 1,
                quote_volume=0.0,
                trade_count=0,
                taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0,
                is_closed=True,
            )
        )
    bars.sort(key=lambda bar: bar.open_time)
    return tuple(bars)


def _table_rows_to_dicts(table: Mapping[str, Any], *, windcode: str) -> list[dict[str, Any]]:
    """把 Wind 的 ``{columns:[{name}], rows:[[...]]}`` 表格转成按列名的字典行。

    列名缺失或重复就直接报错 —— 按位置硬编码列顺序是这类接口最经典的静默错
    （腾讯 ``fqkline`` 就是字段序不是 OHLC，见 ``a_share_public.py``）。
    """
    raw_columns = table.get("columns")
    raw_rows = table.get("rows")
    if not isinstance(raw_columns, Sequence) or not isinstance(raw_rows, Sequence):
        raise WindSourceError(f"Wind 表格结构不符（{windcode}）：columns/rows 不是数组")
    names: list[str] = []
    for column in raw_columns:
        if isinstance(column, Mapping):
            name = column.get("name")
        else:
            name = column
        if not isinstance(name, str) or not name:
            raise WindSourceError(f"Wind 表格列缺少 name（{windcode}）：{column!r}")
        names.append(name)
    if len(set(names)) != len(names):
        raise WindSourceError(f"Wind 表格列名重复（{windcode}）：{names}")
    return [
        dict(zip(names, row, strict=False))
        for row in raw_rows
        if isinstance(row, Sequence) and not isinstance(row, str)
    ]


def _wind_time_to_ms(value: str) -> int:
    """Wind 的日 K ``TIME`` 实测形如 ``2010-01-04T00:00:00.000+02:00``。

    **只取日期部分，绝不按瞬时时刻换算**：``2010-01-04T00:00:00+02:00`` 换算成
    UTC 是 ``2010-01-03T22:00Z``，日期会退一天。日线的 ``open_time`` 口径是
    "交易日 00:00 UTC"（与 ``a_share_local`` 一致），所以先截出前导日期。

    兼容 ``2026-09-24`` / ``20260924`` / ``2026/09/24`` / 带时间与偏移的 ISO。
    """
    text = value.strip()
    match = _DATE_PREFIX_RE.match(text)
    if match is None:
        raise ValueError(f"无法解析 Wind 日期：{value!r}")
    year, month, day = (int(part) for part in match.groups())
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)
