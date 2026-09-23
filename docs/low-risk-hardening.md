# 低危增强项处理记录

日期：2026-09-23

## 已处理

- 关键领域状态字段使用 `Literal` 类型别名：分型、走势类型、结构状态、事件类型、一买状态和背驰状态。
- `StructureElement` 增加级别、方向、时间范围和 high/low 区间校验。
- oracle parity 已由 `.github/workflows/ci.yml` 固定版本 job 覆盖。
- 参考实现许可证边界与 PyArmor blob 状态记录在 `docs/reference-audit.md`：当前仓库未发现可执行 PyArmor blob，因此不伪造 blob 哈希。

## 保留边界

`CanonicalBar` 继续承担输入层完整数值校验；其他结构 dataclass 的构造校验逐步增强，但数据库/外部 JSON 入口仍必须通过 application/adapters 的验证层，不把裸 dataclass 构造当作不可信输入边界。

## 验收

```text
pytest tests -q -rs
ruff check cpt tests scripts/compare_oracle.py
ruff format --check cpt tests scripts/compare_oracle.py
mypy cpt
import-linter lint --config .importlinter
git diff --check
```
