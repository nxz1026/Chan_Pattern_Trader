"""全局测试夹具。

## 为什么必须有一条"按需补因子"的兜底

R17-3 的按需补因子会在本地因子缺失时**联网拉腾讯并写生产库**。默认关闭（见
``cpt.application.a_share_snapshot.factor_ensurer_from_env``），但**生产入口
``cpt.web.a_share_routes.snapshot_payload`` 是显式打开的** —— 于是任何打到
``/api/dashboard/a-share/snapshot`` 的测试都会走真实链路。

这是踩出来的：早期版本把默认写成"开"，跑一次 ``pytest`` 就让
``test_snapshot_reason_no_factor_is_not_db_error`` 真的去腾讯拉了 600519 的 800 行
因子并写进生产库（因子表 94 → 95 只）。所以这里**无条件**把它关掉，需要测试该
路径的用例自己用 ``monkeypatch.setenv(..., "1")`` 显式打开并注入假取数函数。
"""

from __future__ import annotations

import pytest

#: 与 ``cpt.application.a_share_snapshot.ENV_ONDEMAND_FACTOR`` 同值。
#: 这里写字符串字面量而不是 import：conftest 不该把被测包拖进 collection 阶段
#: （CI 模拟里 cpt 的依赖可能被屏蔽）。
_ONDEMAND_ENV = "CPT_ASHARE_ONDEMAND_FACTOR"


@pytest.fixture(autouse=True)
def _disable_ondemand_factor_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """整个测试会话禁止按需补因子：不联网、不写库。"""
    monkeypatch.setenv(_ONDEMAND_ENV, "0")
