"""缠论后端选择开关（R16-4）—— 把 R14 的 czsc 引擎真正接进生产路径。

## 为什么需要这个模块

R14 交付了 :class:`~cpt.adapters.czsc_chanlun.CzscChanlunBackend`（分型/笔换
czsc，中枢仍走 CPT 自研），但**没有任何生产路径用它**——``cpt/web/__main__.py``
与 ``cpt/web/a_share.py`` 都硬编码 ``NativeChanlunBackend()``。后果是看板上画的
仍是 R14 实测判定的"烂笔"（笔端点中位跨度 2 根原始 K 线、短跨度占比 66.9%），
决策 A1 在生产里等于没生效。

本模块提供 ``名字 → 后端实例`` 的唯一入口，供 CLI 的 ``--backend`` 使用。

## 三档取值

+----------+------------------------------------------------------------+
| ``auto`` | 装了 czsc（且版本锁定匹配）就用 czsc，否则回落 native       |
+----------+------------------------------------------------------------+
| ``czsc`` | 强制 czsc；未安装/版本不符时**响亮报错**，不静默回落        |
+----------+------------------------------------------------------------+
| ``native`` | 强制 CPT 自研后端（离线/回放/对照用）                    |
+----------+------------------------------------------------------------+

``auto`` 的回落是刻意的：czsc 是可选依赖（``.[chan]`` extra），CI 只装
``requirements-dev.txt``（**不含 czsc**）。若 ``auto`` 在缺 czsc 时抛错，CI
与本地行为就会分叉；回落 native 保证两边都能跑，只是精度不同。因此**任何测试
都不能断言"auto 一定选中哪个后端"**，只能断言协议兼容性。

## 实测记录（2026-09-24，3 个 oracle fixture）

- native 笔端点中位跨度 **2** 根原始 K 线，跨度 <4 根占比 **66.9%**；
- czsc 笔端点中位跨度 **9–10** 根，跨度 <4 根占比 **6.1%**；
- 中枢：换 czsc 笔后区间宽最小 **4.5 → 17.2**，畸形近零宽度消失。
"""

from __future__ import annotations

from typing import Final

from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.adapters.reference_chanlun import ChanlunBackend

__all__ = [
    "BACKEND_CHOICES",
    "DEFAULT_BACKEND",
    "UnknownBackendError",
    "resolve_backend",
]

#: ``--backend`` 的合法取值（顺序＝帮助信息里的展示顺序）。
BACKEND_CHOICES: Final[tuple[str, ...]] = ("auto", "czsc", "native")

#: 未显式指定时的默认档：装了 czsc 就用 czsc（生产意图），否则回落自研。
DEFAULT_BACKEND: Final[str] = "auto"


class UnknownBackendError(ValueError):
    """``--backend`` 收到非法取值。"""


def resolve_backend(
    name: str = DEFAULT_BACKEND,
    *,
    min_bi_len: int | None = None,
) -> ChanlunBackend:
    """把档位名字解析成后端实例。

    Args:
        name: ``"auto"`` / ``"czsc"`` / ``"native"``（大小写不敏感）。
        min_bi_len: 传给 czsc 后端的笔门槛（去包含后 K 线根数）。``None``
            时用 czsc 上游默认 6。**native 后端忽略本参数**——它的笔口径由
            ``cpt/domain/bi.py`` 决定，不接受该门槛（量纲不同的
            ``min_elements_for_higher_bi`` 是另一回事）。

    Returns:
        :class:`~cpt.adapters.reference_chanlun.ChanlunBackend` 实例。

    Raises:
        UnknownBackendError: ``name`` 不在 :data:`BACKEND_CHOICES` 中。
        cpt.adapters.czsc_chanlun.CzscNotInstalledError: ``name="czsc"`` 但
            czsc 未安装（``auto`` 档不抛，改为回落 native）。
        cpt.adapters.czsc_chanlun.CzscVersionError: ``name="czsc"`` 但版本不符。
    """
    normalized = name.strip().lower()
    if normalized not in BACKEND_CHOICES:
        raise UnknownBackendError(f"未知后端 {name!r}；合法取值：{'/'.join(BACKEND_CHOICES)}")

    if normalized == "native":
        return NativeChanlunBackend()

    # 延迟导入：czsc 是可选依赖，``auto`` 档在未安装时必须能回落而不是
    # 在 import 期就炸掉整个 web 服务。
    from cpt.adapters.czsc_chanlun import (  # noqa: PLC0415
        CzscChanlunBackend,
        CzscNotInstalledError,
        CzscVersionError,
        _import_czsc,
    )

    try:
        # **显式探针**：``CzscChanlunBackend.__init__`` 不做导入（导入在
        # ``compute_structures`` 里），所以「构造成功」不等于「czsc 可用」。
        # 在工厂里先真导入一次，让缺依赖在**服务启动时**就响亮暴露，而不是
        # 等第一张快照静默降级。
        _import_czsc()
    except (CzscNotInstalledError, CzscVersionError):
        if normalized == "czsc":
            raise
        # ``auto``：环境没装/版本不符 → 回落自研后端（可用性优先于精度）
        return NativeChanlunBackend()

    if min_bi_len is None:
        return CzscChanlunBackend()
    return CzscChanlunBackend(min_bi_len=min_bi_len)
