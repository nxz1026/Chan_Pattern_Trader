"""``~/.dbconfig`` 解析与 psycopg3 连接参数的**唯一权威实现**。

本模块是 2026-09-25 审核（``docs/audit/cpt-code-audit-20260925.md`` §5.1）的收口：
合并前同一份逻辑在 ``cpt/adapters/a_share_local.py``、``cpt/adapters/a_share_pool.py``、
``scripts/factor_backfill.py`` 各存在一份，且已经漂移。

## 为什么自带实现，而不 import ``asel``

``asel.storage.dbconfig`` 的同名函数只存在于 ``a_share_emotion_leader/`` 项目里，
CPT 与长龙 venv 都没有该包。``cpt/`` 只依赖 ``psycopg``（可选）+ ``~/.dbconfig``
这一个文件约定，跨项目边界最小。这是**刻意**的，不是遗漏。

## 为什么异常类型由调用方注入

合并前三份的失败异常各不相同：``a_share_local`` 抛 ``AShareLocalError``、
``a_share_pool`` 抛 ``WatchlistError``、``factor_backfill`` 抛 ``SystemExit``。
为**不改动既有行为**（审核铁律：禁止改动既有生产逻辑/顺序），本模块只负责解析与
校验，异常类型通过 ``exc_type`` 注入，不在这里硬编码。
"""

from __future__ import annotations

import pathlib
from typing import Any, Final

__all__ = [
    "DB_CONFIG_FILE",
    "DBConfigError",
    "connection_kwargs",
    "read_dbconfig",
]

#: ``~/.dbconfig`` 位置（``$KEY=value`` 行格式）。
DB_CONFIG_FILE: Final[pathlib.Path] = pathlib.Path.home() / ".dbconfig"


class DBConfigError(RuntimeError):
    """``~/.dbconfig`` 不存在或缺少必需键（未注入 ``exc_type`` 时的默认类型）。"""


def read_dbconfig() -> dict[str, str]:
    """解析 ``~/.dbconfig`` 的 ``$KEY=value`` 行；文件不存在返回空 ``dict``。

    只认以 ``$`` 开头且含 ``=`` 的行；键值两侧空白一律 strip。非 ``$`` 开头的行
    （注释、空行）静默跳过——这是与 ``asel.storage.dbconfig`` 一致的既有口径。
    """
    if not DB_CONFIG_FILE.exists():
        return {}
    out: dict[str, str] = {}
    for line in DB_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("$") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def connection_kwargs(*, exc_type: type[BaseException] = DBConfigError) -> dict[str, Any]:
    """构造 psycopg3 连接参数。

    缺少 ``$RDSHOST`` 或 ``$DB_PW`` 时 ``raise exc_type(...)``；``exc_type`` 由调用方
    注入以保持各自模块原有的异常契约。返回的 dict **不含**任何 ssl 键——需要
    ``sslrootcert`` 的调用方（如 ``a_share_local``）自行在返回值上追加。
    """
    cfg = read_dbconfig()
    if not cfg.get("$RDSHOST") or not cfg.get("$DB_PW"):
        raise exc_type(f"~/.dbconfig 缺失 $RDSHOST 或 $DB_PW。位置: {DB_CONFIG_FILE}")
    return {
        "host": cfg["$RDSHOST"],
        "port": int(cfg.get("$DBPORT", "5432")),
        "dbname": cfg.get("$DBNAME", "longkonglong"),
        "user": cfg.get("$USER", "postgres"),
        "password": cfg["$DB_PW"],
        "connect_timeout": 15,
    }
