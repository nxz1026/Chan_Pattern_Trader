#!/usr/bin/env python3
"""门禁⑫：**变异抽查** —— 验「测试到底有没有在工作」。

## 为什么要有这道

2026-10-06 实测过一件事：``tests/test_min_bi_len_calibration.py::
test_v1_has_no_short_bi_left`` 从上线起就没断言过任何东西 ——
``_span_of`` 查表键用错（拿 ``start_time`` 的表去查 ``bi.end_time``，
而后者取自分型的 ``end_time``，结尾 999999 vs 000000），**永远查不到**，
于是每次走兜底 ``return PROD_GATE``，断言恒真。

而当时仓里已有 **11 道门禁、跑过无数次、全绿**。

## 为什么 11 道门禁全都看不见

它们查的都是**形状**：唯一性、行数、字符串、行号。而恒真断言的**形状是对的**。
更要命的是它们大多只跑 ``pytest`` 看 **rc** —— 恒真断言的 rc 和真断言一样是 0。

⇒ 本门禁**不看测试写了什么，只看它会不会响**：注入一个已知破坏，
如果测试仍然全绿，说明那条不变式**没有任何测试在盯**。

## 与 ``selftest_gates.py`` 的分工

| | ``selftest_gates.py`` | 本脚本 |
|---|---|---|
| 被验对象 | **门禁**（查唯一性/行数的那些） | **测试**（pytest 用例） |
| 验什么 | 「门禁抓不抓得到自己的错例」 | 「测试抓不抓得到自己那条不变式的破坏」 |

两者是同一个思路在两层上的应用，缺任何一层都留死角。

## 为什么是**精选登记表**而不是随机抽样

随机抽 2~3 个用例：命中全靠运气，且 CI 上不可复现。
这里登记的每一条都是**本仓真实出过、或差点出过的 bug 对应的不变式**，
每一条都在 2026-10-06 用手工变异验证过「它确实会转红」。
⇒ 确定性、可复现，且每条都有名字可追。

## 安全约束（重要）

1. **只在干净工作区上跑** —— 启动时 ``git status --porcelain`` 非空就拒绝执行。
   否则本脚本的改动会和你的既有改动混在一起，出问题时无法区分是谁改的。
2. 逐条 ``try/finally`` 还原，跑完再断言工作区仍然干净。
3. 任何一步崩了都**先还原再报错** —— 不留半改状态。

## 退出码

- ``0``：登记的不变式**全部**有测试在盯；
- ``1``：有变异**存活**（= 有不变式没人盯），或本脚本自检失败。

⚠️ 「变异存活」不是 bug，而是**一条关于测试覆盖的发现**。
它的正确处理方式：要么补测试，要么把该条从登记表里删掉并写明理由
（说明这条不变式是刻意不设防的）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(
    # ⚠️ 允许用 ``CPT_REPO`` 覆盖（沿用 deploy/cron 脚本的同名约定）。
    # 原因：``scripts/selftest_gates.py`` 的夹具协议是「把脚本**复制到临时目录**
    # 再改坏它」，而本脚本默认从 ``__file__`` 推仓库根 —— 那样 ROOT 会指向临时目录，
    # 正控制立刻因为「找不到源码文件」而崩。那种崩会让自检报「夹具不对」，
    # 掩盖真正要验的东西。覆盖之后，改坏的副本能对着**真仓库**跑。
    os.environ.get("CPT_REPO") or Path(__file__).resolve().parents[1]
)

#: 单条变异跑测试的超时（秒）。超时要算「没抓到」而不是挂死。
TIMEOUT = 180


@dataclass(frozen=True)
class Mutation:
    """一条「不变式」及其破坏方式。

    :param name: 不变式的名字（报告里用它，也是补测试时的抓手）。
    :param rel: 被改动的文件，相对仓库根。
    :param old: 要替换的原文，必须**唯一命中**。
    :param new: 替换成什么。
    :param tests: 期望因此变红的测试 node id（不给具体用例时给整个文件）。
    :param why: 这条不变式为什么重要 —— 写给将来���过它的人。
    """

    name: str
    rel: str
    old: str
    new: str
    tests: tuple[str, ...]
    why: str


#: 登记表：每条 = 一个已知重要的不变式 + 一个已知能破坏它的变异。
#: ⚠️ 全部条目都在 2026-10-06 手工变异验证过「确实转红」，别随便加没验过的。
MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="recent_runs 排序键用 COALESCE",
        rel="cpt/storage/dashboard_run_store.py",
        old="ORDER BY COALESCE(generated_at, created_at) DESC",
        new="ORDER BY generated_at DESC",
        tests=("tests/test_dashboard_run_store_prune.py",),
        why="PG 的 DESC 默认 NULLS FIRST，NULL 行会吃掉 LIMIT 的槽位"
        "（实测 17 行 NULL ⇒「最近 50 次运行」只剩 33 条真实行）",
    ),
    Mutation(
        name="prune 清理键覆盖 NULL 行",
        rel="cpt/storage/dashboard_run_store.py",
        old="WHERE COALESCE(generated_at, created_at) < now()",
        new="WHERE generated_at < now()",
        tests=("tests/test_dashboard_run_store_prune.py",),
        why="裸 generated_at 命中不了 NULL 行，那批行会永久留存 —— "
        "正是 R56 迁移注释点名要 owner 决定的那件事",
    ),
    Mutation(
        name="prune 失败抛错而非返回 0",
        rel="cpt/storage/dashboard_run_store.py",
        old='_LOG.warning("cpt_dashboard_run 清理失败 keep_days=%s: %s", keep, exc)',
        new=(
            '_LOG.warning("cpt_dashboard_run 清理失败 keep_days=%s: %s", keep, exc)'
            "\n        return 0"
        ),
        tests=("tests/test_dashboard_run_store_prune.py",),
        why="返回 0 与「本来就没有过期行」同值，cron 无法分辨「干完了」"
        "还是「一条都没删掉」，慢性泄漏就在日志里静悄悄地继续",
    ),
    Mutation(
        name="store 层不替调用方 commit",
        rel="cpt/storage/dashboard_run_store.py",
        old="            cur.execute(sql, (keep,))\n            return int(cur.rowcount or 0)",
        new=(
            "            cur.execute(sql, (keep,))"
            "\n            conn.commit()"
            "\n            return int(cur.rowcount or 0)"
        ),
        tests=("tests/test_dashboard_run_store_prune.py",),
        why="事务边界归调用方（见 cpt/storage/__init__.py）。store 替它 commit "
        "会与调用方的多个写入拆成两个事务",
    ),
    Mutation(
        name="清理作业真的登记在 crontab 里",
        rel="deploy/cron/crontab",
        old=(
            "30 4 * * * /home/ubuntu/DSH/Chan_Pattern_Trader/deploy/cron/"
            "dashboard-run-prune-daily.sh"
        ),
        new="# (mutated) removed",
        tests=("tests/test_dashboard_run_store_prune.py",),
        why="R43 实测：run_inspection 从 R38 建好到 R43 从未被调度过，"
        "因为作业只存在于某台机器的 crontab。写完函数不登记等于没写",
    ),
    Mutation(
        name="cron 失败非零退出",
        rel="deploy/cron/dashboard-run-prune-daily.sh",
        old="exit $rc",
        new="exit 0",
        tests=("tests/test_dashboard_run_store_prune.py",),
        why="「跑完了」和「跑挂了」不能同码，否则 cron 把失败当成功",
    ),
    Mutation(
        name="跨度不足时合并端点（门槛的定义）",
        rel="cpt/domain/bi.py",
        old="if span < min_bi_len:",
        new="if False:",
        tests=("tests/test_min_bi_len_calibration.py",),
        why="跨度不足时若改成**丢弃**而不是合并，笔序列在时间轴上开天窗，"
        "后续中枢与背驰的分母就错了。这是 build_bis 的核心不变量",
    ),
    Mutation(
        name="笔的端点映射用对分型字段",
        rel="tests/test_min_bi_len_calibration.py",
        old="end = by_end.get(bi.end_time)",
        new="end = by_start.get(bi.end_time)",
        tests=("tests/test_min_bi_len_calibration.py",),
        why="Bi.start_time 取自起点分型的 start_time、Bi.end_time 取自终点分型的 "
        "end_time（结尾 999999）。查错表 ⇒ 永远查不到 ⇒ 断言恒真 —— "
        "这正是 test_v1_has_no_short_bi_left 长期空转的原因",
    ),
    Mutation(
        name="跨度测量用去包含后根数",
        rel="tests/test_min_bi_len_calibration.py",
        old="    return [_span_of(b, fractals) for b in bis]",
        new="    return [max(1, round((b.end_time - b.start_time) / BAR_MS)) for b in bis]",
        tests=("tests/test_min_bi_len_calibration.py",),
        why="门槛判 merged_index 跨度，而原始根数量纲偏大。量纲错会被自动写进 "
        "calibration-r56-min-bi-len.json，让文档重新漂回「门槛 6 ≈ p80」的错误结论",
    ),
    Mutation(
        name="markFresh 启动轮询",
        rel="dashboard/dash-core.js",
        old="startPolling(url, POLL_INTERVAL_MS);",
        new="/* mutated */",
        tests=("tests/test_dashboard_polling_contract.py",),
        why="2026-10-06 的根因：startPolling 只挂在无缓存分支，稳态轮询全部"
        "命中缓存提前 return，stale 定时器永不重布，15 秒后必然误报「数据陈旧」",
    ),
)


#: 自检用的**负控制**：改一个列出的测试**不会**碰到的文件。
#: 它**必须**被报成「存活」—— 若被报成「抓到」，说明本脚本在乱报。
SELFTEST_UNWATCHED = Mutation(
    name="(自检负控制) 与所列测试无关的改动",
    rel="cpt/domain/config.py",
    old="min_bi_len: int = 6",
    new="min_bi_len: int = 99",
    tests=("tests/test_dashboard_polling_contract.py",),
    why="只读 dashboard/*.js 的契约测试不可能因为改 Python 默认值而变红。"
    "本条用来证明本脚本**能**报出「存活」，而不是无脑报「抓到」",
)


def _dirty() -> list[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _apply(path: Path, m: Mutation) -> None:
    """**字节级**注入变异。

    ⚠️ 必须走二进制：仓里不少文件是 CRLF，而 ``read_text`` / ``write_text``
    在文本模式下会做换行规范化（CRLF→LF）。第一版用文本模式，结果「还原」
    把整个文件改成 LF，``git status`` 立刻变成满屏 modified ——
    **一个专门用来安全还原的脚本，自己还原不干净**。这正是它要抓的那类 bug。
    """
    raw = path.read_bytes()
    old = m.old.encode("utf-8")
    new = m.new.encode("utf-8")
    n = raw.count(old)
    if n != 1:
        raise AssertionError(
            f"变异锚点在 {m.rel} 里命中 {n} 次（要求恰好 1 次）。"
            f"代码变了、或锚点不再是唯一 —— 修登记表，别绕过。"
        )
    path.write_bytes(raw.replace(old, new))


def _run(node_ids: Sequence[str]) -> tuple[bool, int, str]:
    """跑给定测试。返回 (是否失败, rc, 输出尾部)。"""
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *node_ids,
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    proc = subprocess.run(
        argv,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    blob = proc.stdout + proc.stderr
    return proc.returncode != 0, proc.returncode, blob[-800:]


def _restore_side_effects() -> tuple[list[str], list[str]]:
    """还原「跑测试时被测试自己改脏」的**其它**已跟踪文件。

    ## 为什么需要

    有些测试**有写副作用**且写在**被 git 跟踪**的文件里 ——
    ``test_calibration_table_is_written`` 每次跑都重写
    ``docs/calibration-r56-min-bi-len.json``。平时它算出同样的数字所以看不出；
    一旦注入变异，它就会把**错误的数字**写进那份数据（实测：量纲变异会让
    ``v0_median_len`` 从 3 退回 4、``v0_short_lt_gate`` 从 298 退回 232）。
    ⇒ 那不只是「跑完目录脏了」，而是**变异真的污染了产物**。

    ## 为什么可以放心 ``git checkout --``

    因为启动时已断言工作区干净（见 :func:`main`）。⇒ **跑完任何变脏的路径，
    必定是本次跑出来的**，不会是用户自己的改动。这是那个启动断言换来的安全前提。

    返回 (已跟踪脏文件, 跑出来的未跟踪文件)。未跟踪文件**不自动删** ——
    自动删是不可逆操作，万一判错代价太大，只报告。
    """
    tracked: list[str] = []
    untracked: list[str] = []
    for line in _dirty():
        if line.startswith("??"):
            untracked.append(line[3:].strip())
            continue
        tracked.append(line[3:].strip())
    if tracked:
        subprocess.run(
            ["git", "checkout", "--", *tracked],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    return tracked, untracked


def check_one(m: Mutation) -> tuple[bool, str, list[str]]:
    """注入 → 跑测试 → 还原。返回 (是否被抓到, 细节, 被测试写脏并已还原的产物)。

    备份与还原都是**字节级**（见 :func:`_apply` 的说明）：文本模式的换行
    规范化会让「还原」悄悄改写文件行尾，那不是还原。
    还原完还要处理测试的写副作用（见 :func:`_restore_side_effects`）。
    """
    path = ROOT / m.rel
    backup = path.read_bytes()
    try:
        _apply(path, m)
        caught, rc, blob = _run(m.tests)
        detail = f"rc={rc} " + (blob.strip().splitlines()[-1] if blob.strip() else "")
    finally:
        path.write_bytes(backup)
        polluted, _ = _restore_side_effects()
    return caught, detail, polluted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每次运行的输出尾部")
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="只跑本脚本的自检（正控制 + 负控制），不跑完整登记表",
    )
    a = ap.parse_args(argv)

    print("═" * 72)
    print("门禁⑫：**变异抽查** —— 测试到底有没有在工作？")
    print("═" * 72)

    dirty = _dirty()
    if dirty:
        print("❌ 工作区不干净，拒绝执行：")
        for d in dirty[:10]:
            print(f"     {d}")
        print("\n   本脚本会临时改动源码后还原。带着未提交改动跑，")
        print("   出问题时无法区分是你的改动还是本脚本的 —— 先提交或 stash。")
        return 1

    ok = True
    todo = (MUTATIONS[0], SELFTEST_UNWATCHED) if a.selftest else MUTATIONS

    if a.selftest:
        # 正控制必须被抓到，负控制必须被抓不到 —— 两个都对了，机制才算活着。
        pos, pos_detail, _ = check_one(MUTATIONS[0])
        neg, neg_detail, _ = check_one(SELFTEST_UNWATCHED)
        print(f"  {'✅' if pos else '❌'} 正控制：已知不变式被破坏 → 测试转红    {pos_detail}")
        neg_mark = "✅" if not neg else "❌"
        print(f"  {neg_mark} 负控制：无关改动 → 测试**仍绿**（应报存活） {neg_detail}")
        if not pos:
            print("\n  ❌ 正控制失守：登记表第 1 条变异竟然没让测试变红。")
            print("     可能是那条测试也空转了 —— 登记表已不可信。")
        if neg:
            print("\n  ❌ 负控制失守：无关改动被报成「抓到」。")
            print("     本脚本在乱报，不能作为证据。")
        print("─" * 72)
        good = pos and not neg
        print("变异抽查机制**自检通过** ✅" if good else "变异抽查机制**自检失败** ❌")
        return 0 if good else 1

    print(f"  登记表 {len(todo)} 条不变式（每条都在 2026-10-06 手工验证过会转红）\n")
    for m in todo:
        started = time.time()
        try:
            caught, detail, polluted = check_one(m)
        except Exception as exc:  # noqa: BLE001
            print(f"  💥 {m.name:34s} 本脚本崩了: {type(exc).__name__}: {exc}")
            ok = False
            continue
        mark = "✅ 抓到了" if caught else "❌ **没人盯**"
        note = ""
        if polluted:
            note = f"  ⚠️ 并写脏了产物: {', '.join(polluted)}"
        print(f"  {mark:14s} {m.name}  ({time.time() - started:.1f}s, {detail}){note}")
        if not caught:
            ok = False
            print(f"       ↓ {m.why}")
            print(f"       ↓ 变异：{m.rel} 里「{m.old[:48]}」→ 上面那组测试仍然全绿，")
            print("       ↓ 说明这条不变式**没有任何测试在盯**。要么补测试，")
            print("       ↓ 要么从登记表删掉并写明「刻意不设防」的理由。")

    left = _dirty()
    print("─" * 72)
    if left:
        print("❌ 跑完工作区不干净 —— 本脚本没能完全还原：")
        for d in left[:10]:
            print(f"     {d}")
        print("   用 `git checkout -- .` 还原后请查这个脚本。")
        return 1
    print("工作区已还原干净 ✅")
    print("登记的不变式**全部**有测试在盯 ✅" if ok else "有不变式**没有测试在盯** ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
