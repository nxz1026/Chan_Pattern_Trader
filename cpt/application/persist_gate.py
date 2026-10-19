"""「测试别往生产表写」的统一开关。

## 为什么需要它

监控表与事件流都是**旁路**：掉了不影响出图，所以写入失败一律只记日志、不抛。
这个「不抛」在设计上是优点，在测试里却是灾难点 —— 一次真实的落库会**静默成功**，
没有任何断言会红。

已经栽过三次，每一次都比上一次藏得深：

1. **``cpt_run_metric``（R45，2026-10-06）** —— ``cpt/web/app.py`` 的
   ``/api/dashboard/runs`` 写库。修法是 ``CPT_RUN_STORE_PERSIST=0``
   （读写两端都管住），记在 ``docs/known-traps.md`` #28。
2. **``cpt_run_metric`` 的水位行（2026-10-19，审计）** —— ``cpt/web/__main__.py``
   的 ``_record_run_metric`` 是模块级函数、**没有任何环境闸门**，而
   ``tests/test_realtime_poll_degradation.py`` 会构造真 ``_RealtimeProvider``
   并喂给它伪造的 binance 行情。实测：跑该文件一遍，生产库多一行
   ``bi_count=0 / last_bar_time=1700104400000`` 的假水位，反复跑累积到 141 行，
   还让 ``scripts/run_inspection.py`` 报出 21 条 ``counts_jumped`` 假故障。
3. **``cpt_structure_event``（2026-10-19，审计）** —— 第 2 条修完才发现同一类
   还有一条更隐蔽的路径：``tests/test_ashare_placeholder_rows.py`` 用假 client，
   而假 client 的 ``_get_conn()`` 返回 ``None``；``a_share_snapshot`` 把这个
   ``None`` 原样传给 recorder，recorder 的 ``_connection(None)`` 于是
   **自己开了一条生产连接**。「假 client」这个前提在 ``_get_conn()`` 那一层就漏了。

第 2、3 条的共同点：**闸门只守在了其中一条入口上**。所以本模块把开关集中到这里，
任何一处新增的旁路写入都必须先问一句 :func:`prod_table_write_enabled`。

## 开关语义

只有字面 ``"0"`` 表示关闭，其余（包括未设置、空串、``"false"``、``"no"``）
一律**开启**。这与 ``cpt/web/app.py`` 原有实现逐字一致 —— 刻意不做
「非空即真」的宽松解释：宽松会让 ``CPT_RUN_STORE_PERSIST=false`` 意外**打开**
写入，而这个变量的全部用途就是「别写」。不设默认值也因此：默认值一旦被人写错，
错的方向是「悄悄开始写生产表」。

生产环境**从不设置**这个变量，所以生产行为与引入本模块之前完全一致。
"""

from __future__ import annotations

import os
from typing import Final

__all__ = ["RUN_STORE_PERSIST_ENV", "prod_table_write_enabled"]

#: 全仓**唯一**的「旁路表落库」开关名。
#:
#: 名字里的 R45 来历（``run_store``）是历史：它 2026-10-06 为 ``cpt_run_metric``
#: 而设，2026-10-19 扩展到 ``cpt_structure_event``。改名要同步
#: ``tests/conftest.py``、``docs/known-traps.md`` #28、
#: ``docs/db-inventory-and-cleanup.md``，且会让「旧部署里设了旧名」静默失效
#: （旧名不生效 = 继续写生产表），所以保留现名、只把语义写进这里。
RUN_STORE_PERSIST_ENV: Final[str] = "CPT_RUN_STORE_PERSIST"

#: 关闭时用的字面量。不写成 ``bool`` 入参是为了不给调用方「传错类型 = 静默开」的机会。
_OFF: Final[str] = "0"


def prod_table_write_enabled() -> bool:
    """现在允许把旁路表写入生产库吗？默认允许。

    :returns: 环境变量为字面 ``"0"`` 时 ``False``，否则 ``True``。

    调用点应当**在打开连接之前**就问（见 ``cpt/web/__main__.py`` 的
    ``_record_run_metric``）：先开连接再判断，等于把「测试不写库」变成
    「测试仍然要连一次生产库」—— 而连不上的环境会直接报错，测试反而更脆。
    """
    return os.environ.get(RUN_STORE_PERSIST_ENV, "1") != _OFF
