"""``dashboard_run_store`` 的排序键与 **prune 保留期**（R57 新增）。

## 为什么现在才补

R23 建表时写了「append-only + 不自动 GC」，把保留期 SQL 留在迁移注释里::

    DELETE FROM public.cpt_dashboard_run WHERE generated_at < now() - interval '7 days'

2026-10-06 实测：**这条注释从来没变成过任何自动化**。全仓 grep 无
retention/cleanup/purge 逻辑（唯一的 DELETE 是 ``cpt_run_metric`` 的 prune），
生产 crontab 三条 CPT 作业里也没有 —— 这张表**从来没被清理过**，
而它一行约 79 kB（snapshot jsonb 很肥）。

R56/R57 连续两轮处理了「假时间戳」这件事：R56 放开 ``generated_at`` 的 NOT NULL
并让 Python 侧拒绝写假值，R57 补 ``created_at`` 并建
``idx_cpt_dashboard_run_coalesce_time``。**两次都只把路铺好了，没人会走** ——
没有清理作业，索引和列都白搭。

## 本文件钉住什么

1. **排序键必须是 ``COALESCE(generated_at, created_at)``**。PostgreSQL 的
   ``DESC`` 默认 **NULLS FIRST**，裸 ``ORDER BY generated_at DESC`` 会把 NULL 行
   顶到最前面，每条吃掉一个 ``LIMIT`` 槽（实测 17 行 NULL ⇒ 「最近 50 次运行」
   只剩 33 条真实行）。
2. **清理键同样必须是 COALESCE**：裸 ``generated_at < ...`` 命中不了 NULL 行，
   它们会永久留存 —— 正是 R56 迁移注释点名要 owner 决定的那件事。
3. **失败 ≠ 没东西可删**：``prune`` 失败必须抛（与 ``run_metric_store`` 同纪律）。
4. **不 commit**：store 层一律不 commit（见 ``cpt/storage/__init__.py``）。
5. **作业真的被调度了**：断言 ``deploy/cron/crontab`` 里有这一行。
   这条是 R43 那次教训的直接延续 —— 当时 ``run_inspection.py`` 从 R38 建好到
   R43 **从未被调度过**，因为「跑没跑」只存在于 crontab 里，而 crontab 不会被
   review、不会被搜到。写完函数却不登记 crontab，等于没写。

全程离线：用假连接，不碰 DB。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.storage import dashboard_run_store as drs  # noqa: E402


class _Cur:
    rowcount = 0

    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.sql = ""
        self.params: tuple = ()

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.sql = " ".join(sql.split())
        self.params = params
        self.conn.calls.append(self.sql)
        self.rowcount = self.conn.deleted
        if self.conn.raise_on_delete and "DELETE" in self.sql.upper():
            raise self.conn.raise_on_delete
        if self.conn.raise_on_select and "DELETE" not in self.sql.upper():
            raise self.conn.raise_on_select


class _Conn:
    def __init__(
        self,
        *,
        raise_on_delete: Exception | None = None,
        raise_on_select: Exception | None = None,
        deleted: int = 0,
    ) -> None:
        self.calls: list[str] = []
        self.raise_on_delete = raise_on_delete
        self.raise_on_select = raise_on_select
        self.deleted = deleted
        self.commits = 0
        self._cur = _Cur(self)

    def cursor(self) -> _Cur:
        return self._cur

    def commit(self) -> None:
        self.commits += 1


# ─────────────────────────── 排序键（①）───────────────────────────


def test_run_index_orders_by_coalesce_not_bare_generated_at() -> None:
    """``recent_runs`` 的排序键必须是 COALESCE。

    ⚠️ 断言的是**排序键本身**，不是「有没有 ORDER BY」：裸
    ``ORDER BY generated_at DESC`` 一样有 ORDER BY，但在 PostgreSQL 上
    NULLS FIRST 会把 NULL 行顶到最前 —— 只查「有没有排序」会放过它。
    """
    sql = " ".join(drs._RUN_INDEX_SQL.split())
    assert "ORDER BY COALESCE(generated_at, created_at) DESC" in sql
    assert "ORDER BY generated_at DESC" not in sql, (
        "裸 ORDER BY generated_at DESC 在 PostgreSQL 上是 NULLS FIRST，"
        "NULL 行会占掉 LIMIT 的槽位"
    )


def test_run_index_limit_survives_null_rows() -> None:
    """LIMIT 槽位不会再被 NULL 行吃掉：排序键对 NULL 行是 created_at。

    用假连接模拟「表里有 NULL 行」的情形，断言 SQL 走的是能定位 NULL 行的键 ——
    生产上对应 R57 建的 ``idx_cpt_dashboard_run_coalesce_time``。
    """
    conn = _Conn()
    with pytest.raises(drs.DashboardRunError):
        # 假连接 fetchall 没实现，这里只需要 SQL 已经发出去
        drs.recent_runs(conn, limit=50)
    sql = " ".join(conn._cur.sql.split())
    assert "COALESCE(generated_at, created_at)" in sql
    assert 50 in conn._cur.params


# ─────────────────────────── prune（②）───────────────────────────


def test_prune_returns_deleted_count() -> None:
    conn = _Conn(deleted=7)
    assert drs.prune(conn, keep_days=7) == 7
    assert any("DELETE" in c for c in conn.calls)


def test_prune_keys_on_coalesce() -> None:
    """清理键必须覆盖 NULL 行，否则那批行永久留存。"""
    conn = _Conn()
    drs.prune(conn, keep_days=7)
    sql = conn._cur.sql
    assert "DELETE FROM public.cpt_dashboard_run" in sql
    assert "COALESCE(generated_at, created_at) <" in sql, (
        "清理键必须是 COALESCE —— 裸 generated_at < ... 命中不了 NULL 行"
    )
    assert 7 in conn._cur.params


def test_prune_raises_on_failure_not_returning_zero() -> None:
    """失败必须抛：返回 0 会让 cron 打出「已删除 0 行」，像成功的空转。"""
    conn = _Conn(raise_on_delete=RuntimeError("connection lost"))
    with pytest.raises(drs.DashboardRunError):
        drs.prune(conn, keep_days=7)


def test_prune_does_not_commit() -> None:
    """store 层一律不 commit（见 ``cpt/storage/__init__.py``）。

    漏了就是静默回滚：cron 日志会写「已删除 N 行」，而表其实一行没少。
    """
    conn = _Conn(deleted=5)
    drs.prune(conn, keep_days=7)
    assert conn.commits == 0, "store 层替调用方 commit 了 —— 事务边界归调用方"


@pytest.mark.parametrize("bad", [0, -1, "abc", None])
def test_prune_clamps_or_rejects_bad_keep_days(bad: object) -> None:
    """<=0 夹到 1（与 run_metric 一致）；不可解释的值抛。

    ``keep_days=0`` 若被放行，SQL 变成「删掉所有已产生的行」——
    所以它必须是**夹到 1**而不是原样传下去。
    """
    if bad is None:
        with pytest.raises(drs.DashboardRunError):
            drs.prune(_Conn(), keep_days=bad)  # type: ignore[arg-type]
        return
    conn = _Conn()
    try:
        drs.prune(conn, keep_days=bad)  # type: ignore[arg-type]
    except drs.DashboardRunError:
        return
    assert conn._cur.params == (1,), f"keep_days={bad!r} 必须夹到 1"


# ───────────────────── 作业真的被调度了（R43 教训）─────────────────────


def test_cron_job_is_registered_in_repo_crontab() -> None:
    """``prune`` 必须真的出现在 ``deploy/cron/crontab`` 的**作业行**里。

    R43 实测过的坑：``run_inspection.py`` 从 R38 建好到 R43 **从未被调度过**，
    因为作业只存在于某台机器的用户 crontab，仓里没有。「跑没跑」只存在于
    crontab 里 —— 写完函数不登记，等于没写。

    ⚠️ 断言前**必须滤掉注释行**，口径与 ``deploy/cron/crontab`` 文件头里
    规定的那条核对命令一致（``grep -vE '^\\s*(#|$)'``）。

    这是本轮**第三次**栽在同一类错误上（前面两次：
    ① 轮询契约测试的缓存分支断言范围过大，删掉目标行测试仍全绿；
    ② cron 脚本告警断言按整行原文匹配，改长提示语就失败）。
    第一版这里直接 ``assert "dashboard-run-prune-daily.sh" in crontab`` ——
    而文件头的**文档表格**里也写着这个名字，于是把真正的调度行删掉，
    测试依然全绿。**子串断言必须先确定「这段文本代表什么」**，
    否则它匹配到的是同一串字符在别处的合法出现。
    """
    raw = (ROOT / "deploy" / "cron" / "crontab").read_text(encoding="utf-8")
    job_lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    matched = [line for line in job_lines if "dashboard-run-prune-daily.sh" in line]
    assert matched, (
        "清理作业没出现在 deploy/cron/crontab 的作业行里 —— "
        "函数写了也不会有人调用（R43：run_inspection 曾因此从未被调度过）"
    )
    assert any(line.split()[0].isdigit() for line in matched), (
        f"找到的不是调度行（首字段不是分钟数）：{matched}"
    )


def test_cron_script_reads_keep_days_from_env() -> None:
    """窗口必须从环境读，且真的 export 给了内联 Python。

    ``run-metric-prune-daily.sh`` 踩过：变量只进了日志、Python 里硬编码，
    于是「一个只影响日志的配置变量比没有更坏」—— 看日志的人以为窗口改了，
    据此判断「表怎么没小下去」。
    """
    script = (ROOT / "deploy" / "cron" / "dashboard-run-prune-daily.sh").read_text(
        encoding="utf-8"
    )
    assert "export KEEP_DAYS" in script, "KEEP_DAYS 没 export，内联 Python 读不到"
    assert 'os.environ.get("KEEP_DAYS"' in script, "内联 Python 仍在硬编码窗口"
    assert "CPT_PRUNE_DASHBOARD_RUN_DAYS" in script, "没有对外的覆盖变量"


def test_cron_script_reports_failure_loudly() -> None:
    """失败必须非零退出并在日志里留醒目行，「跑完了」与「跑挂了」不能同码。

    ⚠️ 断言打在**标记前缀**上，不是整行原文：那行里还夹着 ``（rc=$rc）——
    dashboard 运行行会继续堆积``，按整行匹配会让「只是把提示语写长了」
    也变成测试失败 —— 那是断言范围比被钉住的行为大。
    """
    script = (ROOT / "deploy" / "cron" / "dashboard-run-prune-daily.sh").read_text(
        encoding="utf-8"
    )
    assert "!!!!! 清理未完成" in script, "失败时没有醒目告警行"
    assert 'if [ "$rc" -ne 0 ]' in script, "告警行没有挂在失败分支上"
    assert "exit $rc" in script, "脚本没有按 rc 退出（失败会被当成成功）"
    assert "flock" in script, "缺 flock ⇒ 上一轮没跑完时这一轮会并发删同一批行"