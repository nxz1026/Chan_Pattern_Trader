# R44 交接：§4 bug 已修，补算与切表的下一步（2026-10-03）

> 接 `docs/handoff-20261003-factor-recompute.md`。那份文档的 §4 bug **已修**（本轮）。
> 生产因子表 `asel.ref_adjust_factor` **仍然一行未动**。

---

## 0. 三分钟版

1. **§4 的 bug 修好了**，在 `fix/r44-eastmoney-no-data` 分支（commit `fdfa666`）。
2. 根因比文档写的更细：东财有**两个** `success:false`，语义相反 ——
   `9201`=查无此记录（正常）、`9501`=报表配置不存在（你请求写错了）。
   只按 `success:false` 判死，会把后者也当成「这家公司没分过红」。
3. 真机验证通过：13 只票跑完 **0 个 fatal**（修复前第 1 只就死）。
4. ⚠️ **我没能上 Oracle** —— 附件里只有 ssh `config`，**没有密钥**
   （`~/.ssh/DJ.pem`、`~/.ssh/oracle.key` 都不存在）。所以：
   **测试没在 Oracle 跑、3036 只没补算、切表没动。**
5. 切表前需要你拍板的事，见 §3。**我的建议：先切 `wired` 桶，暂不切 3036 只占位票。**

---

## 1. 改了什么

`cpt/adapters/eastmoney_actions.py`：新增 `_is_no_data()`，把「服务端明说查无此记录」
与「响应结构坏了」分开。

| 情况 | 修复前 | 修复后 |
|---|---|---|
| `success:false` + `code:9201` +「返回数据为空」+ 空 result | **fatal → 掐停整轮** | 返回空行动列表（合法） |
| `success:false` + `code:9501`「报表配置不存在」 | fatal | fatal（**不变**，这是对的） |
| `success:false` + `9201` 但 message 不对 / result 非空 | fatal | fatal（保守兜底） |
| 非 JSON / `result` 不是 dict / `data` 不是 list | 抛 `AttributeError` → 静悄悄进 failed 列表 | 明确抛 `EastmoneyActionError` |

最后一行是**顺手修的另一个洞**：原代码 `(payload.get("result") or {}).get("data")`
在 `result` 是 list 时抛 `AttributeError`，而那个异常既不属 `fatal_errors` 也不属
`transient_errors`，会被 `main` 的兜底分支当成「这只票失败」——
**一只票的坏数据静悄悄混进 failed 列表**，没人看得见。

### 判据是怎么定的（不是照抄文档）

| 检验 | 方法 | 结果 |
|---|---|---|
| 是限流/抖动吗？ | 600519 连打 25 次 | 25/25 全 28 行，**零波动** ⇒ 确定性 |
| 9201 真的=无分红？ | 新浪财经 `vISSUE_ShareBonus` 交叉核对 | 001239/688031 等确有「分红」页条目，但方案全是**不分配**（送0/转0/派0）或「暂时没有数据」⇒ 从未真正派过 |
| 那 002680 呢？<br>（它有 2016~2018 实施过的分红，东财却回 9201） | 查公司状态 | **「长生退」已退市** ⇒ 东财现行表不收录。002680/600074/000584 同类 |
| 9201 占比多少？ | 185 只**已验证真实存在**的票 | **7.6%** |

⚠️ 我第一轮量到 **68.3%**，那是**错的** —— 采样里混了大量不存在的代码，
而东财对「代码不存在」和「无分红」返回**一模一样**的 9201。已用新浪公司页
逐只验证存在性后重测。**别用那个 68% 的数。**

---

## 2. ⚠️ 我没做的事（因为没有密钥）

附件 `ed1a0500d221f820/config` 里**只有 config，没有私钥**：

```
MISS  ~/.ssh/DJ.pem              (DSH，OMP 的跳板)
MISS  ~/.ssh/oracle.key          (oracle 140.83.62.161)
MISS  ~/.ssh/mimir_ssh_access_key
MISS  ~/.ssh/collector-league
```

`140.83.62.161:22` 端口是通的，但**没有密钥进不去**。所以下面三件都还没做：

- [ ] 在 Oracle 上跑测试基线（`§7` 的 853 tests 基线）
- [ ] 补算 3036 只占位票
- [ ] 任何切表动作

**把密钥放进沙箱（或给我一条能连 oracle 的方式）我就能接着往下走。**

---

## 3. 切表：先定「替换而非合并」，但我建议**分两步**

### 3.1 「替换而非合并」—— 同意，而且理由比文档写的更硬

文档 §5 的 GAP-3 说：8 只以上生产 800 行 / 暂存 666 行，多出的 134 行是
**没有对应 K 线的孤儿日期**（生产因子表从 2023-06-15 起，`daily_bar` 从 2024-01-02 起）。

同意**必须替换**。补充一条独立理由：生产表里有 `source='tx:fqkline'` 的
**2125 只非单调**因子。合并式切换（`INSERT ... ON CONFLICT DO UPDATE`）
会让生产表变成「新旧两套口径混写」—— 而这正是 `bind_state_to_source`
docstring 里警告过的、**静默**的失败模式：三张表和报告都不报错，
只有逐行对账才看得出来。

### 3.2 但「一次全切」我建议改成**分两步**

**理由：切完之后你就没有回头路了。**

现在 2169 只暂存数据里，2069 只（95.4%）判定「重算更好」，这是很强的证据 ——
但它是对**已算的那 2169 只**的结论。而 3036 只占位票**一只都还没算**。
如果你现在把 2169 只切过去，剩下 3036 只占位票仍然 `hfq_factor ≡ 1.0`，
于是库里同时存在三种状态：

```
A. 2169 只  已切，用东财真值（好）
B. 2197 只  仍是 tx:fqkline 旧值，其中 2125 只非单调（坏，但没变坏）
C. 3036 只  仍是占位 1.0（坏，但没变坏）
```

B 和 C 都是**原本就坏**的，切表不会让它们更坏 —— 但它会让 A/B/C 三种口径
同时存在于同一张表里，**下游任何按全表统计的东西（回测、因子分布、
「后复权价」均值）都会被 B/C 拖偏**。而现在至少「全表都是旧口径」是自洽的。

**我的建议：**

1. **先补算 3036 只**（§4 的 bug 已修，这一步现在能跑通了）
2. 补算完**重新出一份对账报告** —— 那时候的 95.4% 才代表**全宇宙**，
   而不是 2169 只的子集
3. 再切表，**一次切干净**

如果你时间紧、想先拿 A 的收益，也可以先切 A，但请在切表脚本里
**显式记录切了哪些 code**，让 B/C 的存在是**已知且可查**的，而不是隐形的。

### 3.3 切表脚本必须带的三个断言

无论分不分步，切表那一刻请把这三条做成**硬断言**，不通过就 rollback：

```sql
-- 断言 1：暂存表不能是「空转」——行数必须与对账报告口径一致
SELECT count(DISTINCT code) FROM asel.ref_adjust_factor_v2;   -- 期望 = 报告里的只数

-- 断言 2：不能有孤儿日期（这正是「必须替换」的理由）
--   期望 0 行；有行说明你误用了合并而不是替换
SELECT count(*) FROM asel.ref_adjust_factor_v2 v
WHERE NOT EXISTS (SELECT 1 FROM public.daily_bar b
                  WHERE b.code = v.code AND b.date = v.trade_date);

-- 断言 3：单调性抽检（后复权因子必须单调不降）
--   期望 0 行；这 2125 只旧值就是栽在这里
SELECT code FROM (SELECT code, trade_date, hfq_factor,
                  lag(hfq_factor) OVER (PARTITION BY code ORDER BY trade_date) prev
                  FROM asel.ref_adjust_factor_v2) t
WHERE prev IS NOT NULL AND hfq_factor < prev * 0.999
GROUP BY code;
```

**切表前先 `pg_dump` 两张表**（`/home/ubuntu/` 下留 tar.gz）。

---

## 4. Oracle 上的执行顺序（密钥到位后）

```bash
# 0. 拉分支（当前在 main 上还没 push）
ssh oracle 'cd ~/DSH/Chan_Pattern_Trader && git fetch origin && \
            git checkout fix/r44-eastmoney-no-data && git pull'

# 1. 先跑测试基线 —— 判断契约有没有被打破要 git stash 后逐条对比失败名单，不要数条数
ssh oracle 'cd ~/DSH/Chan_Pattern_Trader && python -m pytest tests/ -q 2>&1 | tail -30'
#    基线：853 tests / 15 failures / 1 error / 29 skipped / 808 passed

# 2. 新增的这组测试单独跑一遍
ssh oracle 'cd ~/DSH/Chan_Pattern_Trader && python -m pytest tests/test_eastmoney_actions.py -v'

# 3. 小样本冒烟：先只跑 5 只，确认不再 fatal
ssh oracle 'cd ~/DSH/Chan_Pattern_Trader && python scripts/factor_recompute.py \
            --scope placeholder --only 001239,001240,001241,688981,600519 --dry-run -v'

# 4. 冒烟通过再放开全量（--scope placeholder 就是默认，3036 只）
ssh oracle 'cd ~/DSH/Chan_Pattern_Trader && nohup python scripts/factor_recompute.py \
            --scope placeholder > ~/logs/factor-recompute-r44.log 2>&1 &'

# 5. 补算完重新出对账报告（这时候的 95.4% 才是全宇宙口径）
ssh oracle 'cd ~/DSH/Chan_Pattern_Trader && python scripts/factor_report.py | tee \
            ~/logs/factor-report-r44.txt'
```

⚠️ **第 3 步的 `--dry-run` 别省。** 这次 bug 的教训就是「没人值守的长跑
3 分钟内死掉」，而它死得**静悄悄**（只写进 state 文件，不报警）。

⚠️ 顺带：现在 cron 里挂着 `20 2 * * *` 的因子重算。**§4 的 bug 修好之前
不要指望它跑通**（现在它每天都会在同一只票上死）。修好了也要先手动跑一轮
确认，再让 cron 接手。

---

## 5. 仍未决（与 `handoff-20261003` §5 一致，我没动）

- [ ] 30 只「台阶对不齐」（605388 / 601811 / 603259 / 603919）成因未查
- [ ] 992→2125 口径更正的根因在上游 `asel`，另一个仓
- [ ] `derived_bar` 每月 4 月底 1~2% 缺口，ingest 侧未定位
- [ ] ⚠️ **§6 安全：生产库口令 2026-09-30 明文进过 git 历史，且仓库是 PUBLIC。**
      需要 owner 轮换口令。**我没有权限也不该代办 —— 但这条真的不能再拖。**
