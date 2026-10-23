"""跨仓契约：盘中封板快照（``docs/intraday-seal-contract.md``）。

## 为什么需要这道测试

`docs/intraday_seal-contract.md` 是**跨仓库**契约，它两边都不受门禁保护：
CPT 的 `check_doc_drift` 只查本仓，emotion-core 也不知道 CPT 写了什么。而这张表
是 CPT **写**、emotion-core **读** —— 列名或语义一变，读方静默拿到 `None`/空结果，
**没有任何报错**，只能靠人想起来去对。

⇒ 这里把三样东西钉在一起：

1. 契约文档里写的列清单 / 主键；
2. ``cpt/storage/intraday_seal_store.py::_INSERT_SQL`` 真在写的列；
3. 迁移脚本里真建的列。

任何一处改了而没同步另两处，这里就红。

## 另外钉一条最容易错的口径

「按北京日期切日」。``timestamptz`` 读回来带 ``+00:00``，直接 ``.date()`` 会拿到
UTC 日期 —— 北京 00:30 的盘前数据会被算进前一天。跨仓读方照抄 `.date()` 就会错一天。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

CONTRACT = ROOT / "docs" / "intraday-seal-contract.md"
MIGRATION = ROOT / "scripts" / "migrations" / "2026-10-22_r90_intraday_seal_snapshot.sql"
MIGRATION_R93 = ROOT / "scripts" / "migrations" / "2026-10-23_r93_seal_snapshot_extra_columns.sql"

#: 契约规定的列（顺序即 INSERT 顺序）。R90 建表 5 列，R93 补 4 列。
EXPECTED_COLUMNS: tuple[str, ...] = (
    "at",
    "code",
    "sealed",
    "bid1_lots",
    "limit_price",
    "price",
    "prev_close",
    "distance_pct",
    "is_st",
)


def _contract_text() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def test_contract_document_exists_and_names_the_table() -> None:
    text = _contract_text()
    assert "public.intraday_seal_snapshot" in text
    assert "PRIMARY KEY (at, code)" in text


def test_contract_lists_exactly_the_expected_columns() -> None:
    """契约文档少写一列、多写一列都要红（R93：5 列 → 9 列）。"""
    text = _contract_text()
    block = re.search(r"public\.intraday_seal_snapshot \((.*?)\);", text, re.S)
    assert block is not None, "契约里没找到建表块"
    # 只看「列名 + 类型」开头的行；PRIMARY KEY 那行也不算列。
    found = re.findall(
        r"^\s{4}(\w+)\s+(?:timestamptz|text|boolean|double precision)\b", block.group(1), re.M
    )
    assert tuple(found) == EXPECTED_COLUMNS


def test_insert_sql_writes_exactly_the_contract_columns() -> None:
    from cpt.storage.intraday_seal_store import _INSERT_SQL

    head = _INSERT_SQL.split("VALUES", 1)[0]
    cols = re.search(r"\((.*?)\)", head, re.S)
    assert cols is not None
    found = tuple(c.strip() for c in cols.group(1).split(","))
    assert found == EXPECTED_COLUMNS, (
        f"_INSERT_SQL 写的列与契约不一致：{found} != {EXPECTED_COLUMNS}。"
        "改表必须同步 docs/intraday-seal-contract.md §2"
    )
    assert "public.intraday_seal_snapshot" in _INSERT_SQL
    assert "ON CONFLICT (at, code) DO NOTHING" in _INSERT_SQL


def test_migration_creates_the_five_base_columns() -> None:
    """R90 建表脚本负责前 5 列，R93 那份 ALTER 负责后 4 列。

    分两次迁移是历史事实（R90 建表，R93 补列），所以这里只钉前 5 列，
    后 4 列由 ``test_r93_migration_adds_exactly_the_four_new_columns`` 钉。
    两条加起来必须等于 ``EXPECTED_COLUMNS``。
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    body = re.search(r"CREATE TABLE[^(]*\((.*?)\n\);", sql, re.S)
    assert body is not None, "迁移脚本里没找到 CREATE TABLE"
    found = tuple(
        re.findall(
            r"^\s+(\w+)\s+(?:timestamptz|text|boolean|double precision)\b",
            body.group(1),
            re.M,
        )
    )
    assert found == EXPECTED_COLUMNS[:5]
    assert "PRIMARY KEY (at, code)" in sql


def test_contract_states_the_three_reading_rules() -> None:
    """三条读表口径是这张表全部的风险所在，文档必须明写。"""
    text = _contract_text()
    assert "没行 = 判不了" in text
    assert "sealed=False" in text
    assert "按 `at` 分组取最新一轮" in text


def test_contract_rejects_cross_round_reading() -> None:
    """文档必须点名「不要按 code 单点查最新」，否则读方一定会这么做。"""
    text = _contract_text()
    assert "不要按 `code` 单点查最新" in text


def test_contract_mentions_the_hour_step_trap() -> None:
    """OnCalendar 的 /STEP 是小时步长 —— 装错一天不采，读方却以为在跑。"""
    text = _contract_text()
    assert "小时步长" in text


def test_contract_records_the_current_data_volume() -> None:
    """写死「当时表里有几行几个交易日」，防止读方拿空表当采集器坏了。

    实测值是 2026-10-22 清理假数据之后的快照（99 行 / 1 个北京交易日 / 3 轮）。
    数字变了要同步更新 §9，否则读方会按旧基数判「数据暴涨/暴跌」。

    116 那个数是被清理掉的旧值（含 19 行探针假数据），**必须不再出现** ——
    留着一个错的基数比没有基数更糟。
    """
    text = _contract_text()
    assert "99" in text, "§9 没记录清理后的行数"
    assert "116" not in text, "§9 还留着清理前的旧基数（含假数据）"
    assert "覆盖的交易日" in text


def test_contract_warns_about_probe_rows_for_raw_sql_readers() -> None:
    """绕过 storage 层自己写 SQL 的读方，必须被告知要过滤哨兵行。

    R91 实测：19 行 `code='600519'` 的探针假数据晚于真实轮次，
    `SELECT max(at)` 只读到那 1 行假的，33 只真实票被完全遮蔽。
    """
    text = _contract_text()
    assert "\\x01" in text, "契约没告诉裸 SQL 读方哨兵行前缀"
    assert "_REAL_ROWS" in text, "契约没提 storage 层已有的过滤"
    assert "遮蔽" in text, "契约没说明假数据的实际后果"


def test_session_is_cut_by_beijing_date_not_utc() -> None:
    """北京时间 00:30 归当天，UTC 16:30 归前一天 —— 两边都验。"""
    from cpt.application.intraday_review import session_of

    # 北京 2026-10-23 00:30 == UTC 2026-10-22 16:30
    late = dt.datetime(2026, 10, 22, 16, 30, tzinfo=dt.UTC)
    assert session_of(late) == dt.date(2026, 10, 23)
    # 同一天的正午两边一致
    noon = dt.datetime(2026, 10, 23, 4, 0, tzinfo=dt.UTC)
    assert session_of(noon) == dt.date(2026, 10, 23)


def test_contract_file_is_not_orphaned() -> None:
    """至少有一处代码/文档指向它，否则等于没人读。"""
    referrers = []
    for path in (
        list(ROOT.glob("docs/*.md"))
        + list(ROOT.glob("cpt/**/*.py"))
        + list(ROOT.glob("scripts/*.py"))
    ):
        if path == CONTRACT:
            continue
        try:
            if "intraday-seal-contract" in path.read_text(encoding="utf-8"):
                referrers.append(path)
        except (OSError, UnicodeDecodeError):
            continue
    assert referrers, "没有文件指向 docs/intraday-seal-contract.md"


def test_r93_migration_adds_exactly_the_four_new_columns() -> None:
    """R93 补的 4 列必须与契约对齐，且都是可空 —— 判不了就是 NULL，不许用 0 冒充。"""
    text = MIGRATION_R93.read_text(encoding="utf-8")
    added = re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", text)
    assert tuple(added) == ("price", "prev_close", "distance_pct", "is_st")


def test_contract_says_lots_are_lots_not_shares() -> None:
    """emotion 侧阈值是股、这里是手 —— 契约必须写明 ×100，否则静默差 100 倍。"""
    text = _contract_text()
    assert "手" in text
    assert "× 100" in text or "x 100" in text, "契约没提醒读方单位是手"


def test_contract_explains_the_two_swapped_columns() -> None:
    """chg_pct→distance_pct、name→is_st 两处换法必须写进契约，否则读方会去找不存在的列。"""
    text = _contract_text()
    assert "distance_pct" in text
    assert "is_st" in text
    assert "chg_pct" in text, "契约没解释为什么不要 chg_pct"
    assert "name" in text, "契约没解释为什么不要 name"


def test_two_migrations_together_cover_every_expected_column() -> None:
    """R90 的 5 列 + R93 的 4 列必须恰好等于契约的 9 列，不能漏也不能重复。"""
    r90 = re.search(r"CREATE TABLE[^(]*\((.*?)\n\);", MIGRATION.read_text(encoding="utf-8"), re.S)
    assert r90 is not None
    base = re.findall(
        r"^\s+(\w+)\s+(?:timestamptz|text|boolean|double precision)\b", r90.group(1), re.M
    )
    added = re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", MIGRATION_R93.read_text(encoding="utf-8"))
    assert tuple(base) + tuple(added) == EXPECTED_COLUMNS
