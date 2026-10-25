-- R96（2026-10-26）：把 `asel.ref_adjust_factor` 里由腾讯 hfq 反推的因子行重算成东财口径
--
-- ## 起因
--
-- `asel.ref_adjust_factor` 3,484,374 行里有 28,569 行（0.8%）的 `source` 是 `tx:fqkline`
-- —— 由 `cpt/adapters/a_share_factor.py::build_factor_rows` 用
-- `腾讯 hfq 收盘 ÷ 不复权收盘` **逐日相除**得来的因子。
--
-- R95 实测证明这条口径**从构造上就是错的**（600519，40 个交易日）：
--
--   | 比较 | 结果 |
--   |---|---|
--   | 腾讯 qfq vs 腾讯 bfq | 价格逐日完全相同，收益差 0.00e+00 |
--   | 腾讯 hfq vs 腾讯 bfq | 收益 39/39 天全部不一致，最大差 5.03e-03 |
--   | 腾讯 hfq 隐含因子 | 逐日漂 6.978~7.088 |
--   | 东财口径因子 | 恒定 6.39414671 |
--
-- 即腾讯 hfq 与它自家的 qfq/bfq 都对不上 ⇒ 逐日相除得到的是**一条不断漂移的曲线**，
-- 不是分段常数因子。`factor_recompute.py` 的 docstring 早就写着「唯一真值是公司行动本身」。
--
-- ## 实测影响面（比表面的 0.8% 小得多）
--
-- `public.daily_bar` 最早只到 **2024-01-02**，而 28,569 行 tx 因子里
-- **只有 251 行**落在有日线的日期上（其余 28,318 行是 2023-06-02~2024-01-01 的孤儿行，
-- 永远匹配不上日线、也永远不会被消费）。
--
-- 但这 251 行对不上的地方不在数值，而在**数量级**：源切换处相邻日因子跳变最大 **+97.17%**
-- （000002 在 2024-01-02：tx 185.459806 → 东财 363.057863），720 处源切换里 172 处 >1%。
-- 一旦将来日线补到 2023，这批 2 倍量级错的因子会立刻变成 100% 的伪收益。
--
-- ## 做法
--
-- 1. 备份全量 28,569 行到 `asel.ref_adjust_factor_tx_backup_20261026`；
-- 2. 有日线的 251 行：因子与 source 都改成暂存表 `asel.ref_adjust_factor_v2`（东财口径）
--    —— 实测这 251 行**改前就已与东财逐位相同**，这一步只是把过时的 source 标签改对；
-- 3. 无日线的 28,318 行：把腾讯锚点换成**该票最早一个有日线那天的东财因子**（前推恒定）。
--    故意**不做台阶** —— 2023 段的除权前收盘取不到，而
--    `factor_from_actions` 的原则是「宁可少一个台阶，也不用猜的收盘价」。
--    所以 source 明写「缺除权前收盘，不做台阶」，不假装算过。
--
-- 为什么不直接删那 28,318 行：它们只是当下用不到，不是「错得必须消失」。
-- 保留 + 标注 + 换成正确量级，比删除更可逆，也不会让将来补日线的人踩空。
--
-- ## 验收（实跑）
--
-- - 跨界（2024-01-02）因子最大跳变：**+97.17% → 0.000000%**（219 只票）
-- - 有日线的 251 行与东财口径不一致数：**0**
-- - 剩余 `tx:fqkline` 行：**0**
-- - 表总行数不变：3,484,374 → 3,484,374
--
-- ⚠️ 本条**只修数据，不改代码**。产生 tx 行的那台设备仍在：
-- `cpt/application/a_share_snapshot.py::_ensure_factors_and_persist`
-- → `fetch_factor_rows()`（腾讯 hfq）→ `upsert_factor_rows()`（`ON CONFLICT DO UPDATE`）。
-- 要根治得改那条按需落库路径，属于另一件事。

BEGIN;

-- 1) 备份（重复执行会先删掉旧备份，以最新一次为准）
DROP TABLE IF EXISTS asel.ref_adjust_factor_tx_backup_20261026;
CREATE TABLE asel.ref_adjust_factor_tx_backup_20261026 AS
SELECT * FROM asel.ref_adjust_factor WHERE source LIKE 'tx:%';

-- 2) 有日线的行：改用东财口径（因子 + source 一起）
WITH used AS (
    SELECT f.code, f.trade_date
      FROM asel.ref_adjust_factor f
      JOIN public.daily_bar d ON d.code = f.code AND d.date = f.trade_date
     WHERE f.source LIKE 'tx:%'
)
UPDATE asel.ref_adjust_factor f
   SET hfq_factor = v.hfq_factor,
       source     = v.source
  FROM used u
  JOIN asel.ref_adjust_factor_v2 v
    ON v.code = u.code AND v.trade_date = u.trade_date
 WHERE f.code = u.code AND f.trade_date = u.trade_date;

-- 3) 无日线的孤儿行：换成「最早一个有日线那天」的东财因子（前推恒定，不做台阶）
WITH anchor AS (
    SELECT DISTINCT ON (f.code) f.code, f.hfq_factor
      FROM asel.ref_adjust_factor f
      JOIN public.daily_bar d ON d.code = f.code AND d.date = f.trade_date
     WHERE f.source NOT LIKE 'tx:%'
     ORDER BY f.code, f.trade_date ASC
)
UPDATE asel.ref_adjust_factor f
   SET hfq_factor = a.hfq_factor,
       source     = 'eastmoney:events 锚点前推 (R96：该段无日线，缺除权前收盘，不做台阶)'
  FROM anchor a
 WHERE f.code = a.code AND f.source LIKE 'tx:%';

-- 4) 自检：跑完不该还剩 tx 行
DO $$
DECLARE left_over integer;
BEGIN
    SELECT count(*) INTO left_over FROM asel.ref_adjust_factor WHERE source LIKE 'tx:%';
    IF left_over <> 0 THEN
        RAISE EXCEPTION 'R96 未修完：还剩 % 行 tx:fqkline', left_over;
    END IF;
END $$;

COMMIT;
