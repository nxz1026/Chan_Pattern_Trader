# crontab 真相源已迁至 Emotion-CPT

**唯一真相源**：`Emotion-CPT/src/emotion_cpt/deploy/cron/crontab`（随契约包分发）。

本目录此前是**第二份副本**。副本之间漂过一次真实故障：因子重算的日志重定向
在一次改调度时间的提交里被弄丢 ⇒ **因子重算失败彻底静默**，而日报还在扫一个
不存在的日志（看着一切正常）。

现在只有一份。改动请改 Emotion-CPT，然后由 `scripts/install_crontab.sh` 装到线上。
