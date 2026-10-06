"""规则口径配置（可序列化）。

本模块定义缠论计算的口径参数。``v0`` 配置与 ``docs/rules.md`` §9 冻结，
任何修改都必须走 ``v0.x`` 升级流程，并同步 ``docs/rules.md`` 与本文档。

设计约束：配置对象本身不可变（``frozen=True``），序列化走
``to_dict()`` / ``from_dict()``，便于持久化、配置对比与结果元数据落盘。

## R45 更正：「冻结」到底护住了什么（之前这里说得比实际强）

原文只说「冻结」，读者会以为**存量数据**也受保护。实测（2026-10-03），
**三套版本机制各管一段，互不交叉**：

============================  =======================================  ==========
机制                          守什么                                      谁在读
============================  =======================================  ==========
``SCHEMA_VERSION``            **回放 fixture** 反序列化时拒收异版本配置    仅
                              （``replay.py`` → ``from_dict``）          ``from_dict``
``reproducibility.config_hash``  运行历史的「换参数了」比对              ``run_metric``
``reproducibility.rules_version`` 快照元数据里对外展示的口径号            前端
============================  =======================================  ==========

⚠️ **实机核查过的两点**：

1. **在线快照的顶层 ``config`` 是空的**（``None``）。``config`` 只由
   :func:`cpt.application.a_share_snapshot.empty_ashare_snapshot` 填，
   **真实有数据的路径不填**。所以 ``config_version`` **不在**线上快照里 ——
   它只在回放 fixture 这条路上有意义。
2. ``SCHEMA_VERSION`` 原先硬编码为 ``"v0"`` 且 :meth:`from_dict` 会 ``pop`` 掉传入的
   同名字段，**这个版本号永远不会变**。于是「改参数要升版本」这条纪律
   **没有任何机制强制** —— 改了参数、版本号还是 ``v0``，旧 fixture 照样通过
   校验被回放，且不会有任何提示。

   **2026-10-06：升到 ``"v1"``。** 起因是 ``min_bi_len`` 的跨度门槛真正在 native
   后端生效（此前只对 czsc 生效，生产用的 native 一直静默丢弃这个参数）。门槛
   一生效，全部结构（笔 / 中枢 / 走势类型 / 信号 / 水位指纹 / 缓存 run）与 ``v0``
   **不可比**，所以 v0 的回放 fixture 必须作废重建。升版不是形式主义 ——
   它是这次口径变更唯一能挡住「拿 v0 的 fixture 跑 v1 的代码还显示正常」的机制。

**结论**：真正检测口径漂移的是 ``config_hash``（且它确实在
:func:`cpt.application.run_metric` 里被比对）。本模块的
``config_version`` 是**回放入口的单点守卫**，不是全局版本闸门。
改参数时**必须手工**同步 ``config_version``，否则回放静默用旧口径。
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
SCHEMA_VERSION: str = "v1"

#: **按 bar 间隔**分档的底层笔门槛（间隔标签 -> 去包含后 K 线根数）。
#:
#: | 间隔 | 值 | 依据 |
#: |---|---:|---|
#: | ``1d`` | 6 | 与 czsc 上游默认对齐（≈ 一周）。A 股就是日线，**不要动**。 |
#: | ``5m`` | 6 | ⚠️ **暂定值**。实测 p80 = 6（砍掉最短两成），中位跨度 4→9 落在
#:   v0 自然范围内，所以不是错值；但 6 是**为日线选的**，拿来做分钟线偏激进。
#:   样本只有 1000 根 5m ≈ 3.5 天，不足以定档 —— 见
#:   ``docs/calibration-r56-min-bi-len.md``。要改先看那份文档的选档方法。 |
#:
#: 间隔标签用 cron 风格（``1d`` / ``5m`` / ``1h`` …），与 :attr:`RulesConfig.levels`
#: 的级别链是两回事 —— 那个是**级别**，这个是**数据粒度**。
DEFAULT_MIN_BI_LEN_BY_INTERVAL: Final[tuple[tuple[str, int], ...]] = (
    ("1d", 6),
    ("5m", 6),
)


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

            ⚠️ **2026-10-06：这一项现在真的接到 native 了。** 此前它只对
            ``czsc`` 后端生效，生产用的 ``native`` 完全不设门槛 —— 根因是 4 个
            ``resolve_backend(DEFAULT_BACKEND, min_bi_len=...)`` 调用点里
            ``DEFAULT_BACKEND`` 就是 ``native``，参数被**静默丢弃**（当时的
            docstring 写「native 不参与（见后文）」，后文并没有实现）。实测 40 只票
            1360 笔里 35.3% 是跨度 < 4 根的**退化笔**（两个分型共用一根 K 线），
            而力度度量正是一买/一卖**背驰比较的输入**。
            现在经 :func:`cpt.adapters.backend_factory.resolve_backend` →
            ``NativeChanlunBackend`` → :func:`cpt.domain.bi.build_bis` 真正生效，
            门槛不足时**合并**端点而非丢弃该笔（丢弃会让笔序列出空洞，后续中枢与
            背驰的分母就错了）。跨度量纲是**去包含后**的 K 线根数，靠
            :attr:`cpt.domain.models.Fractal.merged_index` 给出；拿 ``bar_index``
            （原始下标）顶替会把门槛算严且与参照侧对不上，所以缺失时直接抛错。

            **升版到 v1 的原因**：门槛生效后全部结构（笔 / 中枢 / 走势类型 /
            信号 / 水位指纹 / 缓存 run）与 v0 **不可比**。
            由 czsc ``check_bi`` 的门槛对齐而来（R14 换引擎时引入）；实测在
            4–7 区间笔数几乎不敏感（3 个 oracle fixture 恒 ~50 笔），故取
            czsc 上游默认 6。
        macd_fast: MACD 快线周期。
        macd_slow: MACD 慢线周期。
        macd_signal: MACD 信号线周期。
        divergence_compare: 背驰比较方式，``"area"``=MACD 柱面积（§9.6 冻结）。
        levels: 级别链。**元素的单位按市场而异**：加密侧是分钟数（``5`` = 5 分钟
            级别）；A 股数据源是**日线**（``daily_bar``），``5`` 是「日线级别」，
            **不是** 5 分钟。域内计算只关心 level 的相对大小、与单位无关，但
            **给人看的标签必须按市场取** —— 用
            :func:`cpt.domain.levels.level_label`，不要自己换算。
            （R28-9 实测：这句话原本无条件写「单位：分钟」，LLM 照着把 A 股日线
            结构讲成了「5 分钟级别」。域内计算没错，错在标签，而错误的标签会被
            人当成结论。）
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
    min_bi_len_by_interval: tuple[tuple[str, int], ...] = DEFAULT_MIN_BI_LEN_BY_INTERVAL
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    divergence_compare: str = "area"
    levels: tuple[int, ...] = (5, 30)
    config_version: str = SCHEMA_VERSION


    def min_bi_len_for(self, interval: str | None) -> int:
        """按 **bar 间隔**取笔门槛；未登记的间隔回落到 :attr:`min_bi_len`。

        ## 为什么要按间隔分档（2026-10-06）

        ``min_bi_len`` 的量纲是「K 线根数」，而一根 K 线代表多长时间**取决于间隔**：
        6 根**日线** ≈ 一周，6 根 **5m** ≈ 30 分钟。同一个数字在两个粒度下是两种
        东西。czsc 上游那个默认 6 是**为日线设计的**，原封不动透传到分钟线上，
        等于把日线门槛套在分钟线上。

        实测（真实 BTCUSDT 5m，1034 笔）：门槛 6 大约砍掉最短两成（p80 = 6），
        中位跨度 4 → 9，**落在 v0 自然范围（max 12~15）之内**，所以不是错值，
        只是**为日线选的、拿来做分钟线偏激进**。真正的雷区是 gate >= 10：中位跨度
        15 已经**超过 v0 的自然最大值**，那时门槛不再筛选而是在**制造**结构。

        用不可变的 tuple of pairs 而不是 ``dict``：dataclass 不允许 mutable default，
        而 ``MappingProxyType`` 会让 :meth:`to_dict` 里的 ``asdict`` deepcopy 炸掉。
        查找走本方法，别直接索引字段。
        """
        if interval:
            for key, value in self.min_bi_len_by_interval:
                if key == interval:
                    return value
        return self.min_bi_len

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
