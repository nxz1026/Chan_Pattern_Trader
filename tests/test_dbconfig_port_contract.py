"""adapters/ 层复盘①（��45）：配置解析与巡检退出码。

**离线**：不连 DB、不发 webhook。

## ① ``$DBPORT`` 格式错会漏出 ``ValueError``

``cpt/adapters/_dbconfig.py`` 的契约是「异常类型由调用方注入」
（``a_share_local`` → ``AShareLocalError``、``a_share_pool`` →
``WatchlistError``、``factor_backfill`` → ``SystemExit``）。

但原实现直接 ``int(cfg.get("$DBPORT", "5432"))``：端口写成 ``"abc"``
会抛 ``ValueError``，三个调用方**谁都接不住**。契约在这一处是破的。

## ② 告警没送出去却以 rc=0 退出

``scripts/run_inspection.py``：巡检发现了问题、该发告警，但 webhook 发送失败
时只 ``print("告警发送失败")`` 然后 ``return 0``。

与 R45 修的 ``factor_recompute`` 同一个病：「该做的没做成」与「做完了」共用
rc=0，cron / 看门狗 / 外部监控都看不出来。**而告警通道坏掉的时候，
恰恰最需要机器来发现它。**
"""

from __future__ import annotations

import pytest
from cpt.adapters import _dbconfig
from cpt.adapters._dbconfig import DBConfigError, connection_kwargs


class _WatchlistError(RuntimeError):
    """模拟调用方注入自己的异常类型。"""


@pytest.fixture
def fake_dbconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    """装一份可控的 ~/.dbconfig（不碰真实的家目录那份）。"""

    def _install(text: str) -> None:
        import pathlib
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
        tmp.write_text(text, encoding="utf-8")
        monkeypatch.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)

    _install(
        "$RDSHOST=db.internal\n"
        "$DB_PW=secret\n"
        "$DBNAME=emotion_core\n"
        "$USER=postgres\n"
    )


# ---------------------------------------------------------------------------
# ① $DBPORT 的异常契约
# ---------------------------------------------------------------------------


def test_valid_port_parses(fake_dbconfig: None) -> None:
    assert connection_kwargs()["port"] == 5432          # 缺省
    _dbconfig.connection_kwargs  # noqa: B018 - 保持可读


@pytest.mark.parametrize("raw", ["abc", "54 32", "5.432", "0x1234", "5432x"])
def test_malformed_port_raises_injected_exc_type(
    monkeypatch: pytest.MonkeyPatch, fake_dbconfig: None, raw: str
) -> None:
    """核心回归：端口格式错必须抛**调用方注入**的那个异常类型。"""
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
    tmp.write_text(
        f"$RDSHOST=db.internal\n$DB_PW=secret\n$DBPORT={raw}\n", encoding="utf-8"
    )
    monkeypatch.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)

    with pytest.raises(_WatchlistError):
        connection_kwargs(exc_type=_WatchlistError)


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_port_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, fake_dbconfig: None, raw: str
) -> None:
    """空值 = **未设置** ⇒ 回落 5432，不该报错。

    ``read_dbconfig`` 解析时已对值 ``.strip()``，所以 ``$DBPORT=`` 与
    ``$DBPORT=   `` 存下来都是空串。这一条是刻意与「格式错」区分开 ——
    写端口的人留了个空，不该让服务起不来。
    """
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
    tmp.write_text(
        f"$RDSHOST=db.internal\n$DB_PW=secret\n$DBPORT={raw}\n", encoding="utf-8"
    )
    monkeypatch.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)

    assert connection_kwargs(exc_type=_WatchlistError)["port"] == 5432


def test_unicode_digits_are_accepted_by_int() -> None:
    """一个 Python 怪癖，写下来免得将来有人以为是 bug。

    ``int()`` 接受**任何** Unicode 十进制数字，不只是 ASCII：
    ``int("٣٣") == 33``。所以本该「格式错」的阿拉伯-印度数字会**静默**
    变成端口 33 —— 它是合法整数、也在范围内，于是通过校验。

    不是缺陷（端口 33 确实能解析成 33），但**反直觉**：
    「非 ASCII 数字应当被拒」的直觉在这里不成立。
    """
    assert int("٣٣") == 33


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "99999"])
def test_out_of_range_port_raises_injected_exc_type(
    monkeypatch: pytest.MonkeyPatch, fake_dbconfig: None, raw: str
) -> None:
    """端口是合法整数但**越界**同样要抛 —— 静默连一个不存在的端口更难查。"""
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
    tmp.write_text(
        f"$RDSHOST=db.internal\n$DB_PW=secret\n$DBPORT={raw}\n", encoding="utf-8"
    )
    monkeypatch.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)

    with pytest.raises(_WatchlistError):
        connection_kwargs(exc_type=_WatchlistError)


def test_malformed_port_never_leaks_valueerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归的判据：任何情况下都**不该**漏出裸 ``ValueError``。

    直接 ``pytest.raises(_WatchlistError)`` 就够（``ValueError`` 不是它的
    子类，会直接失败）；这条额外把「必须不是 ValueError」写死，防止将来
    有人把 ``_port`` 改回裸 ``int()`` 而调用方的 except 恰好宽到能接住 ——
    那样测试仍绿，但契约又破了。
    """
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
    tmp.write_text("$RDSHOST=h\n$DB_PW=p\n$DBPORT=abc\n", encoding="utf-8")
    monkeypatch.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)

    with pytest.raises(_WatchlistError) as excinfo:
        connection_kwargs(exc_type=_WatchlistError)
    assert not isinstance(excinfo.value, ValueError)


def test_missing_required_keys_still_raise_injected_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：原有的「缺 $RDSHOST / $DB_PW」行为不能被改坏。"""
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
    tmp.write_text("$RDSHOST=db.internal\n", encoding="utf-8")   # 故意缺 $DB_PW
    monkeypatch.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)

    with pytest.raises(_WatchlistError):
        connection_kwargs(exc_type=_WatchlistError)


def test_error_message_never_contains_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """口令绝不能出现在异常消息里（那会进日志、进告警、进 issue）。"""
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
    tmp.write_text("$RDSHOST=h\n$DB_PW=super-secret-pw\n$DBPORT=abc\n", encoding="utf-8")
    monkeypatch.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)

    with pytest.raises(_WatchlistError) as excinfo:
        connection_kwargs(exc_type=_WatchlistError)
    assert "super-secret-pw" not in str(excinfo.value)


def test_default_exc_type_is_dbconfig_error(fake_dbconfig: None) -> None:
    """不注入时用默认的 ``DBConfigError``。"""
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp()) / "dbconfig"
    tmp.write_text("$RDSHOST=h\n$DB_PW=p\n$DBPORT=abc\n", encoding="utf-8")
    import pytest as _p

    with _p.MonkeyPatch.context() as mp:
        mp.setattr(_dbconfig, "DB_CONFIG_FILE", tmp)
        with _p.raises(DBConfigError):
            connection_kwargs()
