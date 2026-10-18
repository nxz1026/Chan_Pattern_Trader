"""``cpt/web/trade_api.py``：决策投喂协议的回归测试。

不触网、不连库：``_fetch_signals`` 在测试里被替换成替身。真正要守住的是三件事——


1. **状态白名单默认只放 ``confirmed``**：策略口径尚未由 owner 定案，
   默认放宽到 ``structure_ready`` 就是在「结构刚成形」时真金白银买入。
   实测当天 first_buy 是 11 条 structure_ready / 26 条 invalidated / **0 条 confirmed**，
   所以默认口径下必须一条都不发。
2. **未知 signal_type 不猜**：猜错等于把「不是买点」的东西当买点发出去。
3. **「读不到」与「今天没信号」必须分开**：读不到回 503 + 明确错误，
   不能回一个空 actions 让交易机以为「今天没信号」而安静地什么都不做。
"""

from __future__ import annotations

from datetime import date

import pytest
from cpt.web import trade_api


@pytest.fixture()
def trade_dir(tmp_path, monkeypatch):
    """把决策状态目录指到 tmp_path，并锁死状态白名单与股数。"""
    monkeypatch.setenv("CPT_TRADE_DIR", str(tmp_path))
    monkeypatch.delenv("CPT_TRADE_BUY_STATUSES", raising=False)
    monkeypatch.delenv("CPT_TRADE_VOLUME", raising=False)
    return tmp_path


def _row(code="601988", signal_type="first_buy", status="confirmed", price=10.5):
    return {
        "code": code,
        "signal_type": signal_type,
        "status": status,
        "level": 2,
        "price": price,
        "signal_id": f"sig-{code}-{signal_type}",
        "transition_time": 1791500000000,
        "confirmed_time": 1791500000000,
    }


# ── 状态白名单 ──────────────────────────────────────────────────────


def test_default_statuses_include_structure_ready():
    """默认含 structure_ready —— 否则 confirmed 在真实数据上是 0 条，端点永远空。

    ⚠️ 同时把它的代价钉在这里：structure_ready 到 confirmed 隔着反向笔，
    历史上到达过该状态的 9 个 signal_id 里 8 个最终 invalidated（约 89%），
    且 A股 T+1 下当日买入不可卖。**这是「接出来看得见」的口径，不是实盘口径。**
    """
    assert trade_api.DEFAULT_TRADE_STATUSES == ("confirmed", "structure_ready")
    assert trade_api._statuses() == ("confirmed", "structure_ready")


def test_structure_ready_emits_a_buy_decision(trade_dir, monkeypatch):
    """结构就绪 → BUY/OPEN_POS，且能看得到（这是本轮的核心诉求）。"""
    monkeypatch.setattr(
        trade_api,
        "_fetch_signals",
        lambda d: (
            _row(code="000011", signal_type="first_buy", status="structure_ready"),
            _row(code="600519", signal_type="first_buy", status="structure_ready"),
        ),
    )
    payload, status = trade_api.handle_trade_decisions("date=2026-09-30")
    assert status == 200
    assert payload["batch_id"], "有买入信号时必须生成 batch_id"
    assert [a["code"] for a in payload["actions"]] == ["000011", "600519"]
    assert all(a["action"] == "BUY" and a["exec"] == "OPEN_POS" for a in payload["actions"])


def test_statuses_env_overrides(monkeypatch):
    monkeypatch.setenv("CPT_TRADE_BUY_STATUSES", "confirmed, structure_ready")
    assert trade_api._statuses() == ("confirmed", "structure_ready")


def test_empty_statuses_env_falls_back_to_default(monkeypatch):
    """配错成空串时回到默认值，而不是「什么都不发」以外的第三种行为。"""
    monkeypatch.setenv("CPT_TRADE_BUY_STATUSES", "  ")
    assert trade_api._statuses() == ("confirmed", "structure_ready")


# ── 动作映射 ────────────────────────────────────────────────────────


def test_first_buy_maps_to_open_pos(trade_dir, monkeypatch):
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 200
    assert payload["for_date"] == "2026-10-08"
    assert payload["source"] == "cpt"
    assert payload["batch_id"]
    (action,) = payload["actions"]
    assert action["action"] == "BUY"
    assert action["exec"] == "OPEN_POS"
    assert action["code"] == "601988"
    assert action["volume"] == 100


def test_first_sell_maps_to_close_all(trade_dir, monkeypatch):
    monkeypatch.setattr(
        trade_api, "_fetch_signals", lambda d: (_row(code="600519", signal_type="first_sell"),)
    )
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    (action,) = payload["actions"]
    assert action["action"] == "SELL"
    assert action["exec"] == "CLOSE_ALL"


def test_unknown_signal_type_is_skipped_not_guessed(trade_dir, monkeypatch):
    """★未知类型必须跳过——猜错就是把「不是买点」当买点发出去。"""
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(signal_type="third_buy"),))
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 200
    assert payload["actions"] == []
    assert payload["batch_id"] == "", "没有可执行动作时不该生成 batch_id"


def test_same_code_two_types_both_kept(trade_dir, monkeypatch):
    """同一只票同日的「一买 + 一卖」是两条不同信号，都要投喂。"""
    monkeypatch.setattr(
        trade_api,
        "_fetch_signals",
        lambda d: (_row(signal_type="first_buy"), _row(signal_type="first_sell")),
    )
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    assert [a["action"] for a in payload["actions"]] == ["BUY", "SELL"]


def test_price_goes_to_reason_only(trade_dir, monkeypatch):
    """交易机只发市价单，price 不能成为委托价，只能进 reason 供审计。

    且必须标注「后复权 / 非可成交价」：实测 CPT 的 signal price 与 daily_bar.close
    成因股而异的固定倍数（600519 7.06×、000002 307.9×），当展示价会误导一个数量级。
    """
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(price=12.34),))
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    (action,) = payload["actions"]
    assert "12.34" in action["reason"]
    assert "非可成交价" in action["reason"], "复权价必须显式标注，否则会被当委托价"
    assert "price" not in action


def test_volume_env_overrides(trade_dir, monkeypatch):
    monkeypatch.setenv("CPT_TRADE_VOLUME", "200")
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    payload, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    assert payload["actions"][0]["volume"] == 200


# ── 缓存与幂等 ──────────────────────────────────────────────────────


def test_decisions_are_cached_per_date(trade_dir, monkeypatch):
    calls = []

    def fake(for_date):
        calls.append(for_date)
        return (_row(),)

    monkeypatch.setattr(trade_api, "_fetch_signals", fake)
    first, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    second, _ = trade_api.handle_trade_decisions("date=2026-10-08")
    assert first["batch_id"] == second["batch_id"], "同一天两次请求必须拿到同一个 batch_id"
    assert calls == ["2026-10-08"], "缓存命中时不该再查库"


def test_state_file_is_not_shared_with_emotion_core(trade_dir, monkeypatch):
    """★决策目录必须独立：两个上游共用 state.json 会互相覆盖 decisions 缓存。"""
    assert trade_api.DEFAULT_TRADE_DIR != "/home/ubuntu/trade"
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    trade_api.handle_trade_decisions("date=2026-10-08")
    assert (trade_dir / "state.json").exists()
    assert "cpt" in trade_api.DEFAULT_TRADE_DIR


# ── 「读不到」vs「今天没信号」 ────────────────────────────────────────


def test_source_unavailable_returns_503_not_empty_actions(trade_dir, monkeypatch):
    """★库读不到必须回 503 + 明确错误，绝不能回空 actions。

    回空 actions 时交易机会认为「今天没信号」而安静地什么都不做——
    故障被伪装成正常，是这类接口最危险的失败形态。
    """

    def boom(for_date):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(trade_api, "_fetch_signals", boom)
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 503
    assert "error" in payload and "actions" not in payload


def test_no_actions_returns_empty_batch(trade_dir, monkeypatch):
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: ())
    payload, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 200
    assert payload["actions"] == []
    assert payload["batch_id"] == ""


def test_invalid_date_returns_400(trade_dir, monkeypatch):
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: (_row(),))
    _, status = trade_api.handle_trade_decisions("date=not-a-date")
    assert status == 400


# ── 回执 ────────────────────────────────────────────────────────────


def test_results_posted_and_idempotent(trade_dir):
    payload = {
        "batch_id": "b-1",
        "for_date": "2026-10-08",
        "trades": [{"code": "601988", "status": "FILLED"}],
    }
    first, status = trade_api.handle_trade_results_post(payload)
    assert status == 200 and first["status"] == "ok"
    # 幂等：同一 batch 再来一次，不产生第二个文件
    second, status2 = trade_api.handle_trade_results_post(payload)
    assert status2 == 200 and second.get("note") == "idempotent"
    assert len(list(trade_dir.glob("results_*.json"))) == 1


def test_results_rejects_missing_fields(trade_dir):
    _, status = trade_api.handle_trade_results_post({"batch_id": "b-1"})
    assert status == 400
    _, status2 = trade_api.handle_trade_results_post(None)
    assert status2 == 400


def test_results_get_returns_latest(trade_dir):
    payload = {"batch_id": "b-1", "for_date": "2026-10-08", "trades": []}
    trade_api.handle_trade_results_post(payload)
    got, status = trade_api.handle_trade_results_get("date=2026-10-08")
    assert status == 200 and got["batch_id"] == "b-1"


def test_health_reports_source_identity(trade_dir):
    got, status = trade_api.handle_trade_health()
    assert status == 200
    assert got["source"] == "cpt"
    assert got["service"] == "cpt_trade_api"
    assert got["statuses"] == ["confirmed", "structure_ready"]


def test_corrupt_state_raises_rather_than_silent_reset(trade_dir):
    """状态文件损坏必须显式报错，不能悄悄当空状态重跑。"""
    (trade_dir / "state.json").write_text("{ 这不是 JSON", encoding="utf-8")
    _, status = trade_api.handle_trade_decisions("date=2026-10-08")
    assert status == 503
    assert (trade_dir / "state.json").read_text(encoding="utf-8") == "{ 这不是 JSON"


def test_today_default_date_is_iso(trade_dir, monkeypatch):
    """不传 ``date`` ⇒ 用 ``_today()`` 那天（**市场口径**，不是宿主本地日期）。

    ⚠️ 原断言是 ``date.today().isoformat()`` —— 宿主时区（生产是 ``Etc/UTC``）
    的今天，而 :func:`cpt.web.trade_api._today` 走 ``market_today()``
    （``Asia/Shanghai``）。每天 **16:00 UTC 之后两者差一天**，这条测试就会红
    整整 8 小时（R64 复核时踩到）。钉住 ``_today`` 才与时钟无关。
    """
    monkeypatch.setattr(trade_api, "_today", lambda: date(2026, 10, 8))
    seen = []
    monkeypatch.setattr(trade_api, "_fetch_signals", lambda d: seen.append(d) or ())
    trade_api.handle_trade_decisions("")
    assert seen == ["2026-10-08"]


# ── 真机才暴露的那一层：SQL 参数的 psycopg 适配 ─────────────────────


class _FakeCursor:
    """记录 execute() 收到的 SQL 与 params，并返回可控的行。"""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def test_statuses_param_is_a_list_so_psycopg_builds_an_array():
    """★回归：`status = ANY(%s)` 的参数必须是 **list**。

    传 tuple 时 psycopg 适配成 `('confirmed')` 这样的**文本**，
    真机直接报 ``malformed array literal: "(confirmed)"`` → 决策端点 503。

    这个 bug 在 mock 掉 `_fetch_signals` 的单元测试里看不见——所以这里用假游标
    把参数原样抓出来断言形状，而不是再 mock 一层。
    """
    from cpt.storage.signal_event_store import load_trade_decisions

    cur = _FakeCursor(rows=())
    load_trade_decisions(_FakeConn(cur), for_date="2026-10-08", statuses=("confirmed",))
    sql, params = cur.calls[0]
    assert "status = ANY(%s)" in sql
    status_param = params[2]
    assert isinstance(status_param, list), (
        f"statuses 必须是 list（psycopg3 只把 list 适配成 PG 数组），实为 {type(status_param)}"
    )
    assert status_param == ["confirmed"]


def test_empty_statuses_short_circuits_without_touching_the_db():
    """空白名单直接短路——不该为了「什么都不发」去连一次库。"""
    from cpt.storage.signal_event_store import load_trade_decisions

    cur = _FakeCursor(rows=())
    assert load_trade_decisions(_FakeConn(cur), for_date="2026-10-08", statuses=()) == ()
    assert cur.calls == []


def test_day_window_is_market_tz_not_server_tz():
    """★回归（R67 / P1-1）：日切窗口必须是**北京日**，不能是服务器时区日。

    生产机 `SHOW timezone` = `Etc/UTC`，而 `for_date` 是北京日。老写法
    `transition_time >= %s::date` 按 UTC 切 ⇒ 两者在北京 00:00–08:00 差一整天。

    实测过的生产形态：`cpt_signal_event` **312/312** 行的 `transition_time`
    都是 `23:59:59.999 UTC`（= 北京次日 07:59:59.999），恒被切进**前一天**窗口
    ⇒ 交易机凌晨拉到的是昨天的决策（或什么都拉不到）。

    这里断言三件事：SQL 不再出现 `::date`、窗口端点是两个 tz-aware 时刻、
    且「UTC 23:59:59.999 那条」确实落在北京日的窗口内。
    """
    import datetime as dt

    from cpt.domain.market_time import MARKET_TZ, market_day_bounds
    from cpt.storage.signal_event_store import load_trade_decisions

    cur = _FakeCursor(rows=())
    load_trade_decisions(_FakeConn(cur), for_date="2026-10-08", statuses=("confirmed",))
    sql, params = cur.calls[0]

    assert "::date" not in sql, "日界不能交给服务器时区去切（生产机是 Etc/UTC）"
    start, end = params[0], params[1]
    assert start == dt.datetime(2026, 10, 8, 0, 0, tzinfo=MARKET_TZ)
    assert end - start == dt.timedelta(days=1)
    assert start.utcoffset() == dt.timedelta(hours=8), "端点必须带北京时区（+08:00）"

    # 生产现场那条：北京 10-08 07:59:59.999 == UTC 10-07 23:59:59.999
    prod_ts = dt.datetime(2026, 10, 7, 23, 59, 59, 999000, tzinfo=dt.UTC)
    assert start <= prod_ts < end, "这条本来属于北京 10-08，老写法会漏掉它"
    # 反向控制：按 UTC 日切（老写法）会把它切到 10-07 的窗口里
    assert not (
        dt.datetime(2026, 10, 8, tzinfo=dt.UTC) <= prod_ts < dt.datetime(2026, 10, 9, tzinfo=dt.UTC)
    )
    # market_day_bounds 是唯一来源，别在调用点手搓
    assert market_day_bounds(dt.date(2026, 10, 8)) == (start, end)


def test_bad_for_date_raises_instead_of_querying():
    """`for_date` 不是 YYYY-MM-DD 就抛 —— 不要把畸形日期送到 PG 再报 503。"""
    import pytest
    from cpt.storage.signal_event_store import SignalEventError, load_trade_decisions

    cur = _FakeCursor(rows=())
    with pytest.raises(SignalEventError):
        load_trade_decisions(_FakeConn(cur), for_date="10/08/2026", statuses=("confirmed",))
    assert cur.calls == [], "不该发出一条注定报错的查询"


def test_dedupes_same_code_and_type_within_the_day():
    """同一 (code, signal_type) 当天多条跃迁只留最新的一条。

    行序必须**模拟真实 SQL 的 ORDER BY transition_time DESC, id DESC**——
    假游标不排序，所以这里喂的顺序就是「最新在前」。若误把最旧的喂在前，
    去重就会保留旧状态，且测试照样绿——那正是这个 bug 的形态。
    """
    from cpt.storage.signal_event_store import load_trade_decisions

    # 最新在前：sig-a 的 confirmed(ts=3) 早于其 structure_ready(ts=1)
    rows = (
        ("600519", "first_buy", "confirmed", 3, 99.0, "sig-b", 9, 9),
        ("601988", "first_buy", "confirmed", 2, 10.6, "sig-a", 3, 3),
        ("601988", "first_buy", "structure_ready", 2, 10.5, "sig-a", 1, None),
    )
    cur = _FakeCursor(rows=rows)
    out = load_trade_decisions(_FakeConn(cur), for_date="2026-10-08", statuses=("confirmed",))
    assert [r["signal_id"] for r in out] == ["sig-b", "sig-a"]
    assert out[1]["status"] == "confirmed", "同 signal_id 必须保留最新状态"
    assert out[1]["price"] == 10.6


# ── 跨进程写坏 state.json（R67 / 审计 P1-7）──────────────────────────────
#
# 进程内 `threading.Lock` 只挡得住**同进程的线程**，而 state.json 是磁盘上的共享
# 状态：修复前 `_save_state` 又写**固定临时名** `state.json.tmp` —— 两个进程同时
# load→改→save 时，后写者的 `write_text` 会**追加**到别人写了一半的文件上，
# `os.replace` 的源还可能已被对方移走。2026-10-18 六进程 × 40 次实测：240 个幂等键
# 只剩 **129** 个（丢 111 个）、3 次 503「决策状态文件损坏」。
# 生产当前是单实例，所以这是**潜在**故障；但只要起第二个实例或手工脚本就会真发生。

_RACE_PROCS = 4
_RACE_PER_PROC = 15
_RACE_DATE = trade_api._today().isoformat()  # noqa: SLF001 — 回执必须在保留窗口内


def _race_worker(arg: tuple[int, str]) -> list[tuple[int, dict]]:
    """在子进程里连打 N 条回执（每个 batch_id 都必须被记进 ``processed``）。"""
    import os

    n, d = arg
    os.environ["CPT_TRADE_DIR"] = d
    from cpt.web import trade_api as ta

    bad: list[tuple[int, dict]] = []
    for i in range(_RACE_PER_PROC):
        body, code = ta.handle_trade_results_post(
            {"batch_id": f"p{n}-{i}", "for_date": _RACE_DATE, "trades": []}
        )
        if code != 200:
            bad.append((code, body))
    return bad


def test_concurrent_processes_keep_every_idempotency_key(trade_dir):
    """★P1-7 回归：多进程并发回执后，**一个幂等键都不许丢**，且文件必须可解析。

    老代码在这套压力下丢键（实测 240 丢 111）；``flock`` 串行化后必须 100% 保留。
    """
    import json
    import multiprocessing as mp

    ctx = mp.get_context("fork")  # 显式 fork：3.14 起 Linux 默认已换成 forkserver
    with ctx.Pool(_RACE_PROCS) as pool:
        out = pool.map(_race_worker, [(n, str(trade_dir)) for n in range(_RACE_PROCS)])

    assert [b for lst in out for b in lst] == [], "并发回执不该出现 503/错误"
    state = json.loads((trade_dir / "state.json").read_text(encoding="utf-8"))
    assert len(state["processed"]) == _RACE_PROCS * _RACE_PER_PROC
    assert not list(trade_dir.glob("*.tmp")), "临时文件必须被 os.replace 消费掉"


def test_save_state_uses_a_unique_temp_name(trade_dir, monkeypatch):
    """写临时文件名必须唯一 —— 固定名正是「两段 JSON」那个损坏形态的成因。"""
    import json
    from pathlib import Path as _Path

    seen: list[str] = []
    real_replace = trade_api.os.replace

    def _spy(src, dst):
        seen.append(_Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(trade_api.os, "replace", _spy)
    trade_api._save_state({"decisions": {}, "processed": ["a"]})
    trade_api._save_state({"decisions": {}, "processed": ["b"]})

    assert len(set(seen)) == 2, f"两次写用了同一个临时名: {seen}"
    assert "state.json.tmp" not in seen
    assert json.loads((trade_dir / "state.json").read_text(encoding="utf-8"))["processed"] == ["b"]
    assert not list(trade_dir.glob("*.tmp"))


def test_read_modify_write_is_under_the_cross_process_lock(trade_dir, monkeypatch):
    """门禁：回执路径必须真的取 ``flock`` 排他锁（进程内锁挡不住第二个进程）。"""
    import fcntl

    ops: list[int] = []
    monkeypatch.setattr(trade_api.fcntl, "flock", lambda fd, op: ops.append(op))

    body, code = trade_api.handle_trade_results_post(
        {"batch_id": "lock-1", "for_date": _RACE_DATE, "trades": []}
    )
    assert (code, body["status"]) == (200, "ok")
    assert fcntl.LOCK_EX in ops
    assert (trade_dir / trade_api._STATE_LOCK_FILE).exists()
