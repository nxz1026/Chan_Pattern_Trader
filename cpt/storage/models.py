"""SQLite 表结构（纯 schema 定义，不连接 DB）。

本模块只声明 DDL 常量字符串与表名常量，供 README、迁移文档与测试引用。
实际建库/读写由后续阶段实现，本文件不 ``import sqlite3`` 也不导入任何第三方库。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "v0"

# ---------------------------------------------------------------------------
# 表名常量
# ---------------------------------------------------------------------------

TABLE_RAW_BARS: str = "raw_bars"
TABLE_NORMALIZED_BARS: str = "normalized_bars"
TABLE_STRUCTURE_STATES: str = "structure_states"
TABLE_STRUCTURE_EVENTS: str = "structure_events"
TABLE_SIGNALS: str = "signals"
TABLE_LLM_CALLS: str = "llm_calls"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_RAW_BARS: str = """\
CREATE TABLE raw_bars (
    bar_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                 TEXT    NOT NULL,
    interval_minutes       INTEGER NOT NULL,
    open_time              INTEGER NOT NULL,
    close_time             INTEGER NOT NULL,
    open                   REAL    NOT NULL,
    high                   REAL    NOT NULL,
    low                    REAL    NOT NULL,
    close                  REAL    NOT NULL,
    volume                 REAL    NOT NULL,
    quote_volume           REAL    NOT NULL,
    trade_count            INTEGER NOT NULL,
    taker_buy_base_volume  REAL    NOT NULL,
    taker_buy_quote_volume REAL    NOT NULL,
    is_closed              INTEGER NOT NULL DEFAULT 1,
    fetched_at             INTEGER NOT NULL,
    UNIQUE (symbol, interval_minutes, open_time)
)
"""

_DDL_NORMALIZED_BARS: str = """\
CREATE TABLE normalized_bars (
    norm_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_bar_id INTEGER NOT NULL REFERENCES raw_bars (bar_id),
    direction  INTEGER NOT NULL,
    bar_hash   TEXT    NOT NULL,
    UNIQUE (raw_bar_id)
)
"""

_DDL_STRUCTURE_STATES: str = """\
CREATE TABLE structure_states (
    structure_id   TEXT    PRIMARY KEY,
    level          INTEGER NOT NULL,
    kind           TEXT    NOT NULL,
    direction      INTEGER NOT NULL,
    start_time     INTEGER NOT NULL,
    end_time       INTEGER NOT NULL,
    status         TEXT    NOT NULL,
    revision       INTEGER NOT NULL,
    first_seen_at  INTEGER NOT NULL,
    confirmed_at   INTEGER,
    invalidated_at INTEGER,
    source_ids     TEXT    NOT NULL,
    payload        TEXT    NOT NULL DEFAULT '{}',
    updated_at     INTEGER NOT NULL
)
"""

_DDL_STRUCTURE_EVENTS: str = """\
CREATE TABLE structure_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    structure_id TEXT    NOT NULL REFERENCES structure_states (structure_id),
    event_type   TEXT    NOT NULL,
    revision     INTEGER NOT NULL,
    payload      TEXT    NOT NULL,
    occurred_at  INTEGER NOT NULL,
    recorded_at  INTEGER NOT NULL DEFAULT (
        CAST(strftime('%s', 'now') || substr(strftime('%f', 'now'), 4) AS INTEGER)
    )
)
"""

_DDL_INDEX_EVENTS: str = """\
CREATE INDEX idx_events_sid_time
    ON structure_events(structure_id, occurred_at, event_id)
"""

_DDL_SIGNALS: str = """\
CREATE TABLE signals (
    signal_id         TEXT    PRIMARY KEY,
    level             INTEGER NOT NULL,
    signal_type       TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    structure_id      TEXT    NOT NULL,
    center_ids        TEXT    NOT NULL,
    divergence_status TEXT    NOT NULL,
    alert_time        INTEGER,
    candidate_time    INTEGER,
    confirmed_time    INTEGER,
    invalidated_time  INTEGER,
    price             REAL    NOT NULL,
    source_revision   INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
)
"""

_DDL_LLM_CALLS: str = """\
CREATE TABLE llm_calls (
    call_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_hash    TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cost_amount    REAL,
    cost_currency  TEXT,
    response_hash  TEXT,
    called_at      INTEGER NOT NULL
)
"""


def ddl_statements() -> tuple[str, ...]:
    """返回全部 DDL(6 表 + 1 索引),顺序即创建顺序。"""
    return (
        _DDL_RAW_BARS,
        _DDL_NORMALIZED_BARS,
        _DDL_STRUCTURE_STATES,
        _DDL_STRUCTURE_EVENTS,
        _DDL_INDEX_EVENTS,
        _DDL_SIGNALS,
        _DDL_LLM_CALLS,
    )


__all__ = [
    "SCHEMA_VERSION",
    "TABLE_RAW_BARS",
    "TABLE_NORMALIZED_BARS",
    "TABLE_STRUCTURE_STATES",
    "TABLE_STRUCTURE_EVENTS",
    "TABLE_SIGNALS",
    "TABLE_LLM_CALLS",
    "ddl_statements",
]
