# cpt/domain → 已迁至 Emotion-CPT

**判据唯一真源**：`emotion_cpt.structure`（仓：`nxz1026/Emotion-CPT`）。

本目录现在只剩一个 `__init__.py`，作用是**把 17 个子模块别名到真源**：

```python
import cpt.domain.market_time as mt
import emotion_cpt.structure.market_time as real
assert mt is real          # True —— 同一个模块对象
```

因此 CPT 现有的 117 个 `from cpt.domain.X import ...` 调用点**一行都不用改**，
而判据只有一份实现。

> 不要在这里**再加**模块文件：一旦有同名 `.py` 存在，它会与别名机制打架，
> 造成「两个模块对象、状态分叉」—— 那是壳方案的老毛病。
