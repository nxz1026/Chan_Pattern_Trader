"""规则口径配置（可序列化）。

本模块定义缠论计算的口径参数。``v0`` 配置与 ``docs/rules.md`` §9 冻结，
任何修改都必须走 ``v0.x`` 升级流程，并同步 ``docs/rules.md`` 与本文档。

设计约束：配置对象本身不可变（``frozen=True``），序列化走
``to_dict()`` / ``from_dict()``，便于持久化、配置对比与结果元数据落盘。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Self

__all__ = [
    "SCHEMA_VERSION",
    "RulesConfig",
    "default_rules_config",
]

#: 配置口径版本号，写入每个 JSON 导出元数据。
SCHEMA_VERSION: str = "v0"


@dataclass(frozen=True, slots=True)
class RulesConfig:
    """可序列化的规则口径配置。

    ``v0`` 配置冻结。字段语义详见 ``docs/rules.md`` §9；工程参数
    （``min_elements_for_higher_bi``、``min_bi_len``）同样冻结，修改需走升级流程。

    注意两个「笔门槛」量纲不同，**不可混用**：``min_bi_len`` 是底层笔的
    **去包含后 K 线根数**；``min_elements_for_higher_bi`` 是高级别笔包含的
    **低级别结构元素数**。

    Attributes:
        contain_direction: 包含关系方向，``"forward"``=向前（新高新低后处理），
            ``"backward"``=向后。
        fx_qy_middle: 分型严格定义（顶底分型中间 K 线需严格高低点）。
        fx_qj_ck: 分型穿越检查（分型间是否存在穿越）。
        bi_type_new: 新笔类型（True=新笔）。
        zs_wzgx: 中枢位置关系，``"zgd"``=高点比 zg、低点比 zd（§9.5 冻结）。
        zs_level_count: 中枢级别数。
        min_elements_for_higher_bi: 高级别笔最少结构元素（§9.7 冻结，工程参数）。
            **量纲＝低级别结构元素数**，与 ``min_bi_len`` 不同，不可混用。
        min_bi_len: 底层笔最少跨度，**量纲＝去包含后的 K 线根数**（§9.8）。
            由 czsc ``check_bi`` 的门槛对齐而来（R14 换引擎时引入）；实测在
            4–7 区间笔数几乎不敏感（3 个 oracle fixture 恒 ~50 笔），故取
            czsc 上游默认 6。
        macd_fast: MACD 快线周期。
        macd_slow: MACD 慢线周期。
        macd_signal: MACD 信号线周期。
        divergence_compare: 背驰比较方式，``"area"``=MACD 柱面积（§9.6 冻结）。
        levels: 级别链，元素为分钟级别（单位：分钟）。
        config_version: 配置版本号，写入每个计算结果元数据。
    """

    contain_direction: str = "forward"
    fx_qy_middle: bool = True
    fx_qj_ck: bool = True
    bi_type_new: bool = True
    zs_wzgx: str = "zgd"
    zs_level_count: int = 1
    min_elements_for_higher_bi: int = 5
    min_bi_len: int = 6
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    divergence_compare: str = "area"
    levels: tuple[int, ...] = (5, 30)
    config_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """序列化为普通 ``dict``（含 ``config_version``）。"""
        data: dict[str, Any] = asdict(self)
        data["levels"] = list(self.levels)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从 ``dict`` 反序列化。

        - 未知键被忽略，向前兼容（旧数据落入新版配置时丢弃多余字段）。
        - ``levels`` 归一为 ``tuple[int, ...]``。
        - ``config_version`` 不接受数据覆盖——版本号只跟随代码（防止 fixture
          写 ``"config_version": "v99"`` 绕过 v0 冻结契约）。
        - 值域校验由 ``__post_init__`` 兜底。
        """
        if not isinstance(data, dict):
            raise TypeError(f"RulesConfig.from_dict 需要 dict, 收到 {type(data).__name__}")
        levels = data.get("levels", cls.__dataclass_fields__["levels"].default)
        if not isinstance(levels, (list, tuple)):
            raise TypeError(f"levels 必须是 list/tuple, 收到 {type(levels).__name__}")
        kwargs = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        kwargs["levels"] = tuple(int(x) for x in levels)
        kwargs.pop("config_version", None)  # 版本号只跟随代码
        return cls(**kwargs)

    def __post_init__(self) -> None:
        """值域校验：v0 冻结口径的可执行约束。"""
        if self.config_version != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 config_version={self.config_version!r};"
                f" 期望 {SCHEMA_VERSION!r}。升级请走 v0.x 流程并同步 docs/rules.md。"
            )
        if self.macd_fast >= self.macd_slow:
            raise ValueError(f"macd_fast({self.macd_fast}) 必须小于 macd_slow({self.macd_slow})")
        if self.macd_signal <= 0:
            raise ValueError(f"macd_signal 必须正整数, 实测 {self.macd_signal}")
        if not self.levels:
            raise ValueError("levels 必须为非空级别链")
        if any(x <= 0 for x in self.levels):
            raise ValueError(f"levels 必须为正整数, 实测 {self.levels}")
        if self.min_elements_for_higher_bi < 1:
            raise ValueError(
                f"min_elements_for_higher_bi 必须 >= 1, 实测 {self.min_elements_for_higher_bi}"
            )
        if self.min_bi_len < 1:
            raise ValueError(f"min_bi_len 必须 >= 1, 实测 {self.min_bi_len}")
        if self.zs_wzgx not in {"zgd", "zdg", "ggd", "ddd"}:
            # 实际可用档位由 rules.md §9.5 决定,此处仅做白名单最小校验
            raise ValueError(f"zs_wzgx 必须是已知档位之一, 实测 {self.zs_wzgx!r}")

    def __str__(self) -> str:
        """可读单行形式：``RulesConfig(v0): field=value ...``。

        用 ``dataclasses.fields`` 遍历而非硬编码字段清单,新增字段不会
        静默漏出。
        """
        parts = [f"{f.name}={getattr(self, f.name)!r}" for f in fields(self)]
        return f"RulesConfig({self.config_version}): " + " ".join(parts)


def default_rules_config() -> RulesConfig:
    """返回 ``v0`` 默认配置（全部字段取冻结默认值）。"""
    return RulesConfig()
