# 部署真相源已迁至 Emotion-CPT

**唯一真相源**：`Emotion-CPT/src/emotion_cpt/deploy/systemd/`（随契约包分发，任何装了
`emotion-cpt` 的仓都能读到）。

迁移日期：2026-10-24。此前本目录是**第二份副本** —— 线上 `/etc/systemd/system`
与仓内副本曾严重漂移过（2026-10-07 审计记录），而副本之间又各自漂了一次。

现在：
- 只有一份 unit 真相源，在 Emotion-CPT 里；
- `emotion_cpt.cli.check_unit_paths` 门禁做**双向对账**（仓内 ↔ 线上）；
- 任何改动请改 Emotion-CPT，然后由 `scripts/install_units.sh` 装到线上。

> 装之前请先看 `Emotion-CPT/deploy/systemd/README.md` 的说明。
