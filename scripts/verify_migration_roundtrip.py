#!/usr/bin/env python
"""迁移 ↔ 回滚的**往返逐字节比对**（一次性数据迁移的验收工具）。

## 为什么需要它

数据迁移最容易骗过自己的地方是**验收只看行数**。本仓 R67 的 `structure_id`
迁移就踩到了：行数完美还原（20207 → 1990 → 20207），但**480 行的 payload 对不上**
—— 而差分只出现在 jsonb 里，`count(*)` 永远看不见。

真正的判据是「回滚之后，表里每一行的每一个字段都与迁移前**逐字节相同**」。
本脚本就做这一件事。

## 安全设计（这脚本会跑 DELETE，必须保证不可能真的删掉生产数据）

1. **剥掉脚本自带的 `BEGIN;` / `COMMIT;`**，由 Python 控制单个外层事务；
   剥完还残留任何 `COMMIT` 就直接退出 —— 「我以为它没有 COMMIT」是这类
   工具最危险的一厢情愿。
2. 全程只有一次 `conn.rollback()`，**没有任何 commit 路径**。
   PG 里 DDL 也是事务性的，所以建的备份表、索引、删除的行全部随回滚消失。
3. 即使进程被杀：未提交的事务在连接断开时被 PG 回滚，生产数据不受影响。

## 用法

```bash
.venv/bin/python -m scripts.verify_migration_roundtrip \\
    scripts/migrations/2026-10-19_r67_symbol_in_id.sql \\
    scripts/migrations/rollback/2026-10-19_r67_symbol_in_id_rollback.sql
```

退出码：0 = 逐字节还原；1 = 有差异（并打印前几条字段级差异）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

#: 参与比对的列。顺序固定，`_snapshot` 与差异打印共用。
_COLUMNS: tuple[str, ...] = (
    "id",
    "structure_id",
    "event_type",
    "status",
    "revision",
    "payload::text",
    "occurred_at",
    "created_at",
)

#: 被本工具操作的唯一一张业务表。表名写死（而非参数化）是刻意的：
#: 这脚本会 DELETE，绝不能让调用方把目标指向别的表。
_TABLE = "public.cpt_structure_event"


def _top_level_transaction_control(sql: str) -> list[tuple[int, str]]:
    """列出**顶层**（美元引号之外）的事务控制语句，返回 ``(行号, 词)``。

    必须区分「顶层事务控制」与「`DO $$ … $$` 块里的 `BEGIN` / `END`」——
    PL/pgSQL 块自己的 `BEGIN` 长在美元引号里，动它会把整块打成语法错误
    （实测：`syntax error at or near "*"`，因为 `DO $$` 体被劈成裸 SQL）。
    所以按美元引号状态逐行扫描。
    """
    found: list[tuple[int, str]] = []
    in_dollar_quote = False
    for number, line in enumerate(sql.splitlines(), 1):
        # 美元引号可以在任意位置开合，且**同一行内可开可合**
        # （`DO $$ DECLARE` 开、`END $$;` 合）。数一数本行出现几次即可翻转状态。
        # 注意顺序：先记录「本行起始时是否在引号内」，再翻转。
        starts_inside = in_dollar_quote
        for _ in range(line.count("$$")):
            in_dollar_quote = not in_dollar_quote
        if starts_inside or in_dollar_quote:
            continue
        code = line.split("--", 1)[0].strip().rstrip(";").strip().upper()
        if code in {"BEGIN", "COMMIT", "START TRANSACTION", "END"}:
            found.append((number, code))
    return found


def _strip_transaction_control(sql: str) -> str:
    """去掉**顶层**的 `BEGIN;` / `COMMIT;`，换来「由我们控制事务」。

    :raises SystemExit: 剥完仍残留顶层事务控制语句 —— 宁可不跑，也不能让迁移
        自己 commit 掉一份尚未回滚的删除。
    """
    drop = {number for number, _ in _top_level_transaction_control(sql)}
    kept = [line for number, line in enumerate(sql.splitlines(), 1) if number not in drop]
    body = "\n".join(kept)

    # 二次确认：复用**同一套**美元引号感知的判据（先前这里用了不带引号状态的
    # 朴素扫描，把 `DO $$ … BEGIN … $$` 块里的 `BEGIN` 也当成了顶层事务控制 ——
    # 于是每次都被自己的守卫拦下，工具完全无法使用）。
    residual = _top_level_transaction_control(body)
    if residual:
        words = ", ".join(f"L{number}:{code}" for number, code in residual)
        raise SystemExit(f"剥离后仍残留顶层事务控制语句（{words}）—— 拒绝执行")
    return body


def _snapshot(cur: Any) -> dict[int, tuple[Any, ...]]:
    """整表快照，按 id 索引。"""
    cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} ORDER BY id")
    return {row[0]: row for row in cur.fetchall()}


def _digest(rows: dict[int, tuple[Any, ...]]) -> str:
    """行集合的指纹（与 :func:`_snapshot` 的排序无关）。"""
    ordered = [rows[k] for k in sorted(rows)]
    return hashlib.sha256(repr(ordered).encode()).hexdigest()[:16]


def _diff(
    before: dict[int, tuple[Any, ...]], after: dict[int, tuple[Any, ...]]
) -> tuple[int, int, int, list[tuple[int, str, Any, Any]]]:
    """逐行逐字段比对。

    :returns: ``(缺失行数, 多出行数, 字段不同数, 前几条样本)``。
        样本里 ``(id, 列名, 原值, 回滚值)``，值截断到 200 字符。
    """
    only_before = set(before) - set(after)
    only_after = set(after) - set(before)
    samples: list[tuple[int, str, Any, Any]] = []
    changed = 0
    for row_id in sorted(set(before) & set(after)):
        old, new = before[row_id], after[row_id]
        if old == new:
            continue
        for index, (x, y) in enumerate(zip(old, new, strict=True)):
            if x == y:
                continue
            changed += 1
            if len(samples) < 5:
                samples.append((row_id, _COLUMNS[index], str(x)[:200], str(y)[:200]))
    return len(only_before), len(only_after), changed, samples


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        raise SystemExit(2)

    migration_path, rollback_path = Path(argv[1]), Path(argv[2])
    migration = _strip_transaction_control(migration_path.read_text(encoding="utf-8"))
    rollback = _strip_transaction_control(rollback_path.read_text(encoding="utf-8"))

    import psycopg  # noqa: PLC0415
    from cpt.adapters._dbconfig import connection_kwargs  # noqa: PLC0415

    with psycopg.connect(**connection_kwargs()) as conn:
        with conn.cursor() as cur:
            before = _snapshot(cur)
            print(f"迁移前 : {len(before):>7} 行 sha={_digest(before)}")

            cur.execute(migration)
            middle = _snapshot(cur)
            cur.execute(f"SELECT count(*) FROM {_TABLE}")
            total_row = cur.fetchone()
            assert total_row is not None  # count(*) 恒返回一行
            total = int(total_row[0])
            print(f"迁移后 : {len(middle):>7} 行 sha={_digest(middle)}  表内共 {total} 行")

            cur.execute(rollback)
            after = _snapshot(cur)
            print(f"回滚后 : {len(after):>7} 行 sha={_digest(after)}")

        # 唯一的收尾 —— 没有 commit 路径，这就是「生产零改动」的全部依据。
        conn.rollback()

    missing, extra, changed, samples = _diff(before, after)
    print()
    print(f"行数还原  : {'✅' if len(after) == len(before) else '❌'}")
    print(f"逐字节还原: {'✅' if _digest(before) == _digest(after) else '❌'}")
    if missing or extra or changed:
        print(f"  仅迁移前有 {missing} 行 / 仅回滚后有 {extra} 行 / 字段不同 {changed} 处")
        for row_id, column, old, new in samples:
            print(f"    id={row_id} [{column}]\n      原: {old}\n      回: {new}")
    print("\n(已整体 ROLLBACK，生产零改动)")
    return 0 if not (missing or extra or changed) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
