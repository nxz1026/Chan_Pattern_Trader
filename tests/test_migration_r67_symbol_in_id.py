"""R67 / 审计 P0-2 数据迁移的回归钉子（需要真实 Postgres）。

守的是**迁移本身**，不是 `structure_id_of` —— 后者已由
``tests/test_structure_events.py`` 的纯函数用例钉住（新的 id 一定会带 symbol）。
这里守的是更难的那一半：**已经把 20207 行生产数据按老格式写坏的表，怎么治好**。

## 为什么必须有这个文件

R67 写迁移时踩到两次「差点骗过自己」，两次都只有逐行比对才看得见：

1. **只看行数会骗过自己**。回滚第一版跑出 20207 → 1990 → 20207，行数完美还原，
   但 **480 行的 payload 对不上**（差分只藏在 jsonb 里，`count(*)` 永远看不见）。
2. **回滚用「重算」而不是「还原」也会骗过自己**。把 payload.id 就地
   `jsonb_set` 成正确值，得到的是一份「迁移前那 480 行本来就不一致」的历史所没有的
   数据 —— 回滚语义是回到原状，**包括原状里那个不一致**。

所以这里的核心断言是**逐字节往返**，不是行数。

## 隔离手法（与 ``tests/test_storage_llm_call_recovery.py`` 同款）

迁移脚本自带的 `BEGIN;` / `COMMIT;` 被剥掉，由本文件控制单个外层事务，
断言在同一事务内完成，收尾 `rollback()`。**没有任何 commit 路径** —— 这也是
测试敢在共享生产库上跑的原因（PG 的 DDL 同样是事务性的，建的备份表也随回滚消失）。

## 合成「迁移前」数据集（R67 上线后补的）

第一版用例直接拿**生产库当时的真实数据**当迁移前输入，于是 R67 一上线，
本文件整片变红：每条用例都先跑一遍迁移，而守卫正确地拒绝了重复执行
（`已有 1992 行是真·5 段`）。那不是用例写错，是**用例把「库还没迁」当成了前提** ——
迁移的回归钉子必须能一直跑下去，而不是只在「准备迁移的那一刻」有值。

现在改为：在事务内把 `cpt_structure_event` 换成一份**合成的**迁移前数据
（十几行，覆盖全部病灶），迁移机制类用例从此与生产库当前状态无关。
生产库的真实行数/坏损程度仍由 `scripts/verify_migration_roundtrip.py` 在
迁移前实测过一遍（往返逐字节一致），本文件负责钉住**机制**。

⚠️ 夹具还必须把三张真实备份表**清空**、并**删掉真实唯一索引**（都在事务内）：
真实迁移跑完后 `_id_backup_20261019` 里有 2409 行、`_dropped_20261019` 里有 17800 行，
`CREATE TABLE IF NOT EXISTS` 会跳过建表、于是校验步「备份数 ≠ 待改数」直接抛错；
唯一索引则会让合成数据里那组**刻意的重复**插不进去。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = _ROOT / "scripts" / "migrations" / "2026-10-19_r67_symbol_in_id.sql"
_ROLLBACK = (
    _ROOT / "scripts" / "migrations" / "rollback" / "2026-10-19_r67_symbol_in_id_rollback.sql"
)

#: 参与往返比对的列。与 ``scripts/verify_migration_roundtrip.py`` 的 ``_COLUMNS`` 同序。
_COLUMNS = (
    "id",
    "structure_id",
    "event_type",
    "status",
    "revision",
    "payload::text",
    "occurred_at",
    "created_at",
)

#: 段数（`:` 分隔）——R67 之后一律 5 段 `{market}:{symbol}:{kind}:{level}:{start_time}`。
_SEGMENTS = 5


def _require_db() -> bool:
    return os.environ.get("CPT_REQUIRE_DB", "").strip().lower() in {"1", "true", "yes"}


def _open_pg_conn() -> Any:
    """照 ``tests/test_track.py`` 的模式：``CPT_REQUIRE_DB=1`` 时连不上就是红。"""
    try:
        import psycopg  # noqa: F401 — 只探可用性
    except ImportError as exc:
        message = f"psycopg 不可用（需要 `pip install -e '.[db]'`）: {exc}"
        if _require_db():
            pytest.fail(message)
        pytest.skip(message)

    from cpt.adapters._dbconfig import connection_kwargs

    try:
        return psycopg.connect(**connection_kwargs())
    except Exception as exc:  # noqa: BLE001
        message = f"DB not reachable: {type(exc).__name__}: {str(exc)[:120]}"
        if _require_db():
            pytest.fail(message)
        pytest.skip(message)


def _strip_transaction_control(sql: str) -> str:
    """去掉**顶层**的 `BEGIN;` / `COMMIT;`，换来「由测试控制事务」。

    必须区分顶层事务控制与 `DO $$ … $$` 块里的 `BEGIN` / `END` —— PL/pgSQL 块
    自己的 `BEGIN` 长在美元引号里，删掉会把整块打成语法错误（实测
    `syntax error at or near "*"`，因为 `DO $$` 体被劈成了裸 SQL）。
    """
    drop: set[int] = set()
    in_dollar_quote = False
    for number, line in enumerate(sql.splitlines(), 1):
        starts_inside = in_dollar_quote
        for _ in range(line.count("$$")):
            in_dollar_quote = not in_dollar_quote
        if starts_inside or in_dollar_quote:
            continue
        code = line.split("--", 1)[0].strip().rstrip(";").strip().upper()
        if code in {"BEGIN", "COMMIT", "START TRANSACTION", "END"}:
            drop.add(number)

    kept = [line for number, line in enumerate(sql.splitlines(), 1) if number not in drop]
    body = "\n".join(kept)

    # 二次确认：复用同一套美元引号感知的判据。先前这里用过朴素扫描，结果把
    # `DO $$ … BEGIN … $$` 里的 BEGIN 也算成顶层事务控制 —— 自己的守卫把自己拦住。
    residual: list[str] = []
    in_dollar_quote = False
    for number, line in enumerate(body.splitlines(), 1):
        starts_inside = in_dollar_quote
        for _ in range(line.count("$$")):
            in_dollar_quote = not in_dollar_quote
        if starts_inside or in_dollar_quote:
            continue
        code = line.split("--", 1)[0].strip().rstrip(";").strip().upper()
        if code in {"BEGIN", "COMMIT", "START TRANSACTION", "END"}:
            residual.append(f"L{number}:{code}")
    if residual:
        raise AssertionError(f"剥离后仍残留顶层事务控制语句（{', '.join(residual)}）")
    return body


_SQL_MIGRATION = _strip_transaction_control(_MIGRATION.read_text(encoding="utf-8"))
_SQL_ROLLBACK = _strip_transaction_control(_ROLLBACK.read_text(encoding="utf-8"))

_TABLE = "public.cpt_structure_event"


def _snapshot(cur: Any) -> dict[int, tuple[Any, ...]]:
    cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} ORDER BY id")
    return {row[0]: row for row in cur.fetchall()}


def _digest(rows: dict[int, tuple[Any, ...]]) -> str:
    return hashlib.sha256(repr([rows[k] for k in sorted(rows)]).encode()).hexdigest()


def _migration_state(cur: Any) -> str:
    """这张表现在是「迁移前」还是「迁移后」？

    判据与迁移脚本的前置守卫 1 **同源**（连那 84 行测试污染的豁免都一致）：
    真·5 段行存在 ⇔ 迁移已经跑过了。
    """
    cur.execute(
        f"""SELECT count(*) FROM {_TABLE}
             WHERE array_length(string_to_array(structure_id, ':'), 1) = %s
               AND NOT (structure_id LIKE 'cn:600519:%%'
                        AND created_at = TIMESTAMPTZ '2026-10-19 01:16:05.604669+00')""",
        (_SEGMENTS,),
    )
    return "after" if cur.fetchone()[0] else "before"


# ---------------------------------------------------------------------------
# 合成「迁移前」数据集
# ---------------------------------------------------------------------------
#
# 病灶必须**全部**覆盖，否则迁移的分支就有无人走过的路：
#   · crypto 4 段行（回填分支）——含**一组** `(structure_id, revision)` 重复，
#     payload 逐字节相同（去重分支的前提）
#   · cn 4 段行（删除分支）
#   · `cn:600519:*` 5 段测试行（守卫 1 的豁免分支）
#   · 一行 payload.id 停在 3 段（R27-4 旧伤 → 同步分支）
#
# id 从 900001 起，远离生产序列（`last_value=20720`），即使事务意外提交也一眼可辨。
_FAKE_TS = datetime(2026, 10, 18, tzinfo=UTC)
#: 测试污染行的时间戳 —— 必须与迁移脚本守卫 1 里写的那个**完全相等**（微秒级），
#: 否则守卫不豁免它、迁移一上来就被自己的测试垃圾挡住。
_TEST_TS = datetime(2026, 10, 19, 1, 16, 5, 604669, tzinfo=UTC)

_FAKE_ROWS: tuple[tuple[int, str, str, str, int, str, datetime, datetime], ...] = (
    # (id, structure_id, event_type, status, revision, payload_id, occurred_at, created_at)
    (
        900001,
        "crypto:fractal:5:1700000000000",
        "created",
        "confirmed",
        1,
        "crypto:fractal:5:1700000000000",
        _FAKE_TS,
        _FAKE_TS,
    ),
    # ⋯ 与上一行同 (structure_id, revision) 且 payload 逐字节相同 ⇒ 并发双写
    (
        900002,
        "crypto:fractal:5:1700000000000",
        "created",
        "confirmed",
        1,
        "crypto:fractal:5:1700000000000",
        _FAKE_TS,
        _FAKE_TS,
    ),
    (
        900003,
        "crypto:bi:5:1700003600000",
        "created",
        "confirmed",
        2,
        "crypto:bi:5:1700003600000",
        _FAKE_TS,
        _FAKE_TS,
    ),
    # payload.id 停在 3 段（连 crypto: 都没有）—— R27-4 留下来的旧伤
    (
        900004,
        "crypto:zhongshu:5:1700007200000",
        "updated",
        "forming",
        1,
        "zhongshu:5:1700007200000",
        _FAKE_TS,
        _FAKE_TS,
    ),
    # cn 4 段：**必删**（一个 id 横跨多只票的那种坏）
    (
        900005,
        "cn:bi:5:1782172800000",
        "created",
        "confirmed",
        1,
        "cn:bi:5:1782172800000",
        _FAKE_TS,
        _FAKE_TS,
    ),
    (
        900006,
        "cn:fractal:5:1782172800000",
        "created",
        "confirmed",
        1,
        "cn:fractal:5:1782172800000",
        _FAKE_TS,
        _FAKE_TS,
    ),
    # 测试污染的 5 段行：守卫 1 必须靠 created_at + symbol 豁免它
    (
        900007,
        "cn:600519:fractal:5:1797206400000",
        "created",
        "confirmed",
        1,
        "cn:600519:fractal:5:1797206400000",
        _TEST_TS,
        _TEST_TS,
    ),
)

_BACKUP_TABLES = (
    "cpt_structure_event_id_backup_20261019",
    "cpt_structure_event_dropped_20261019",
    "cpt_structure_event_deduped_20261019",
)


def _install_fake_pre_migration(cur: Any) -> None:
    """把表换成合成的「迁移前」状态（全部在调用方的事务内，随回滚消失）。

    三件必须做的事，缺一件都会让迁移在真实库上跑不通：
      1. 删掉真实备份表 —— 它们在上线后**有数据**（2409 / 17800 / 417 行），
         而迁移脚本用 `CREATE TABLE IF NOT EXISTS`，会跳过建表，
         于是第 3 步校验「备份数 ≠ 待改数」直接抛错。
      2. 删掉真实唯一索引 —— 合成数据里那组**刻意的重复**否则插不进去。
      3. 清空事件表 —— 否则合成行与真实 1992 行混在一起。
    """
    for table in _BACKUP_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS public.{table}")
    cur.execute("DROP INDEX IF EXISTS public.uq_cpt_structure_event_sid_rev")
    cur.execute(f"DELETE FROM {_TABLE}")

    for row in _FAKE_ROWS:
        cur.execute(
            f"""INSERT INTO {_TABLE}
                    (id, structure_id, event_type, status, revision, payload,
                     occurred_at, created_at)
                VALUES (%s, %s, %s, %s, %s, jsonb_build_object('id', %s::text), %s, %s)""",
            (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]),
        )
    # 序列推到合成 id 之外，免得迁移里任何 `nextval` 撞上合成主键
    cur.execute(
        f"SELECT setval('public.cpt_structure_event_id_seq', (SELECT max(id) FROM {_TABLE}), true)"
    )


@pytest.fixture
def conn() -> Any:
    """一条全程 rollback 的连接，且表已被换成合成的「迁移前」数据集。

    ⚠️ 为什么不再直接用生产库当时的真实数据：R67 一上线，本文件第一版**整片变红** ——
    每条用例都先 `execute(_SQL_MIGRATION)`，而守卫正确地拒绝了重复执行
    （`已有 1992 行是真·5 段`）。那不是用例写错，是**用例把「库还没迁」当成了前提**。
    迁移的回归钉子必须能一直跑下去，而不是只在「准备迁移的那一刻」有值。

    生产库的真实坏损程度由 `scripts/verify_migration_roundtrip.py` 在迁移前实测过
    （往返逐字节一致）；本文件从此只负责钉住**机制**，与库的当前状态无关。
    """
    connection = _open_pg_conn()
    try:
        with connection.cursor() as cur:
            _install_fake_pre_migration(cur)
            assert _migration_state(cur) == "before", "合成数据集装完仍不是「迁移前」"
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def live_conn() -> Any:
    """一条全程 rollback 的连接，**不**改数据、两种状态都跑。"""
    connection = _open_pg_conn()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# 1. 往返逐字节：**这是本文件的核心断言**
# ---------------------------------------------------------------------------


def test_roundtrip_is_byte_identical(conn: Any) -> None:
    """迁移 → 回滚之后，每一行的每一个字段都必须与迁移前逐字节相同。

    行数相同**不算过** —— R67 第一版正是行数完美却有 480 行 payload 不同。
    """
    with conn.cursor() as cur:
        before = _snapshot(cur)
        assert before, "表是空的 —— 这个钉子失去意义，请确认连的是有数据的那张表"

        cur.execute(_SQL_MIGRATION)
        cur.execute(_SQL_ROLLBACK)
        after = _snapshot(cur)

    assert after.keys() == before.keys(), "回滚后 id 集合变了"
    differing = [k for k in before if before[k] != after[k]]
    if differing:  # pragma: no cover — 失败路径，排查时才有用
        sample = differing[0]
        for index, (x, y) in enumerate(zip(before[sample], after[sample], strict=True)):
            if x != y:
                pytest.fail(
                    f"{len(differing)} 行内容不同，首条 id={sample} 列={_COLUMNS[index]}\n"
                    f"  迁移前: {str(x)[:300]}\n  回滚后: {str(y)[:300]}"
                )
    assert _digest(after) == _digest(before), "sha 不同但逐行相同 —— 比对逻辑有 bug"


# ---------------------------------------------------------------------------
# 2. 迁移之后：表真的被治好了
# ---------------------------------------------------------------------------


def test_every_id_has_a_symbol_segment(conn: Any) -> None:
    """迁移后全是 5 段；第 2 段（symbol）非空且不含分隔符。"""
    with conn.cursor() as cur:
        cur.execute(_SQL_MIGRATION)
        cur.execute(
            f"""SELECT structure_id FROM {_TABLE}
                 WHERE array_length(string_to_array(structure_id, ':'), 1) <> %s""",
            (_SEGMENTS,),
        )
        wrong = [row[0] for row in cur.fetchall()]
        assert not wrong, f"迁后仍有非 {_SEGMENTS} 段的 id（{len(wrong)} 条）：{wrong[:3]}"

        cur.execute(f"SELECT count(*) FROM {_TABLE} WHERE split_part(structure_id, ':', 2) = ''")
        assert cur.fetchone()[0] == 0, "迁后仍有 symbol 段为空的 id"


def test_payload_id_is_synced_with_the_column(conn: Any) -> None:
    """★**这个副本曾经腐坏过两个版本**（known-traps #57）。

    `state_to_payload` 把 `state.id` 也写进 jsonb，于是同一个 id 在表里有两份。
    R27-4 那次的迁移只改 `structure_id` 列、没同步 payload，于是 727 行的
    `payload->>'id'` 停在 3 段形态（连 `crypto:` / `cn:` 前缀都没有），
    活了两个版本没人发现 —— 因为**读路径没有任何判据读它**。

    没有判据的地方，错误不会自己冒出来。所以这里补上判据。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_MIGRATION)
        cur.execute(
            f"SELECT count(*) FROM {_TABLE} WHERE payload->>'id' IS DISTINCT FROM structure_id"
        )
        bad = cur.fetchone()[0]
        assert bad == 0, f"迁后有 {bad} 行的 payload.id 与 structure_id 列不一致"


def test_migration_collapses_the_duplicate_revision_groups(conn: Any) -> None:
    """同一 ``(structure_id, revision)`` 只能有一行 —— 并发双写要收敛成一条。

    A 股侧原来的串号让 592 组出现重复（cn 175 + crypto 417，每组恰好 2 行）。
    去重前会断言这些重复的 payload **逐字节一致**（是同一份结果写了两遍，
    而不是两个不同结果撞号）—— 若不一致，迁移会自己报错停下。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_MIGRATION)
        cur.execute(
            f"""SELECT count(*) FROM (
                    SELECT structure_id, revision FROM {_TABLE}
                     GROUP BY 1, 2 HAVING count(*) > 1) t"""
        )
        assert cur.fetchone()[0] == 0, "迁后仍存在同 id 同 revision 的重复行"


def test_crypto_rows_keep_their_btcusdt_attribution(conn: Any) -> None:
    """crypto 段回填后 symbol 全是 BTCUSDT —— 回填 100% 成立的依据被钉住。

    迁移的前置守卫会查 `cpt_run_metric` 的 crypto 段是否只有 BTCUSDT；
    若哪天多出别的交易对，守卫直接抛错，不会猜着回填。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_MIGRATION)
        cur.execute(
            f"""SELECT DISTINCT split_part(structure_id, ':', 2) FROM {_TABLE}
                 WHERE split_part(structure_id, ':', 1) = 'crypto'"""
        )
        symbols = {row[0] for row in cur.fetchall()}
        assert symbols <= {"BTCUSDT"}, f"crypto 段出现了非 BTCUSDT 的 symbol: {symbols}"


# ---------------------------------------------------------------------------
# 3. 守卫：不能重复执行 / 回滚幂等
# ---------------------------------------------------------------------------


def test_migration_refuses_to_run_twice(conn: Any) -> None:
    """第二遍必须被前置守卫拦住，而不是把 id 再加一段。

    少了这道守卫，重复执行会得到 `crypto:BTCUSDT:BTCUSDT:bi:5:…` —— 每轮都被
    当成新结构，`created` 无限追加。这正是 R27-4 那个「永远匹配不上的 id」的病。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_MIGRATION)
        with pytest.raises(Exception) as excinfo:
            cur.execute(_SQL_MIGRATION)
        assert "不能重复执行" in str(excinfo.value)


def test_rollback_is_idempotent(conn: Any) -> None:
    """回滚跑两遍结果不变 —— 实测成立（第二遍的 UPDATE/INSERT 全部命中不了）。

    证明它值得，因为回滚脚本**不**自带幂等守卫（它有 `ON CONFLICT DO NOTHING`
    与按 `new_id` 反查，天然幂等）。这里把这个性质钉住，免得日后有人「优化」
    成非幂等的写法而无人察觉。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_MIGRATION)
        cur.execute(_SQL_ROLLBACK)
        once = _snapshot(cur)
        cur.execute(_SQL_ROLLBACK)
        twice = _snapshot(cur)

    assert _digest(twice) == _digest(once), "回滚不幂等 —— 二次执行改变了数据"


def test_invariant_holds_for_the_live_table_after_migration(live_conn: Any) -> None:
    """整张表满足五条不变量 —— **两种状态下都跑**，这正是它存在的理由。

    迁移前：先在事务内跑一遍迁移，再验（验的是「迁移的结果」）。
    迁移后：直接验现存数据（验的是「生产库真的被治好了」）——
    R67 上线后这条是唯一还在实际守护生产数据的用例。

    区别于此前的几条：它不针对某个市场、也不依赖「表还是 4 段」这个前提。
    """
    with live_conn.cursor() as cur:
        state = _migration_state(cur)
        if state == "before":
            cur.execute(_SQL_MIGRATION)
        checks = {
            "非5段行": (
                f"""SELECT count(*) FROM {_TABLE}
                     WHERE array_length(string_to_array(structure_id, ':'), 1) <> 5"""
            ),
            "symbol段为空": (
                f"SELECT count(*) FROM {_TABLE} WHERE split_part(structure_id, ':', 2) = ''"
            ),
            "symbol段含分隔符": (
                f"SELECT count(*) FROM {_TABLE} WHERE split_part(structure_id,':',2) LIKE '%:%'"
            ),
            "payload.id不同步": (
                f"SELECT count(*) FROM {_TABLE} WHERE payload->>'id' IS DISTINCT FROM structure_id"
            ),
            "同id同revision重复": (
                f"""SELECT count(*) FROM (SELECT structure_id, revision FROM {_TABLE}
                     GROUP BY 1,2 HAVING count(*) > 1) t"""
            ),
            "未来时间戳": f"SELECT count(*) FROM {_TABLE} WHERE occurred_at > now()",
        }
        broken = {}
        for name, sql in checks.items():
            cur.execute(sql)
            count = cur.fetchone()[0]
            if count:
                broken[name] = count

    assert not broken, f"迁移后仍有不变量不成立：{broken}"


# ---------------------------------------------------------------------------
# 4. 迁移脚本自身的静态纪律（不需要 DB —— 这些在无库的 CI 上也跑）
# ---------------------------------------------------------------------------


def test_rollback_script_exists_and_restores_payload() -> None:
    """回滚必须用**备份的原值**还原 payload，不能就地 `jsonb_set` 重算。

    重算会把那 480 行「本来就不一致」的 payload 一并修好 —— 于是往返比对
    永远差那几百行，而**行数一模一样**。这是 R67 第一版真实踩到的坑。
    """
    text = _ROLLBACK.read_text(encoding="utf-8")
    assert "old_payload" in text, "回滚没引用备份的 payload 原值"
    assert "SET structure_id = b.old_id" in text.replace(
        "structure_id=b.old_id", "structure_id = b.old_id"
    )


def test_migration_documents_the_three_hard_facts() -> None:
    """迁移的注释里必须留着三条硬事实 —— 它们是日后判断「能不能简化」的依据。

    删掉注释不会让代码变红，但会让下一个人以为「回填 crypto 是拍脑袋的」。
    """
    text = _MIGRATION.read_text(encoding="utf-8")
    assert "74 只" in text, "缺少「一个 id 横跨 74 只票」这个定性依据"
    assert "592" in text, "缺少 revision 冲突组数"
    assert re.search(r"75\.2%", text), "缺少「批量反推只能覆盖 75.2%」这个定量依据"
