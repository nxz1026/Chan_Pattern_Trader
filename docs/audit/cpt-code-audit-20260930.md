# CPT 代码审计报告（复审）— 2026-09-30

> - 审计日期：2026-09-30
> - 审计范围：`cpt/` 包、`dashboard/` 前端、`deploy/` 部署配置、`scripts/`（抽样）
> - 审计方式：逐文件静态审查（安全 / 并发 / 错误处理 / 规范 / 可维护性），**不含功能性审核**（按需求排除）
> - 基线：本报告是 [`cpt-code-audit-20260925.md`](./cpt-code-audit-20260925.md) 的复审增量；与基线重叠的结论标注「基线已记录，本次复核仍成立」

---

## 0.0 销账状态（2026-10-01 回填）

> ⚠️ **本报告正文写于报告当时，下面的「修复建议」是当时的待办，不是现状。**
> 报告 20:35 写完，**20:58 就已修复**（commit `4a5a2f7`），M1 之后又跟了两次
> （`b27a0a8` 改 400 → `470737a` 改 415）。**报告一直没回填，导致只读本文的人
> （包括 2026-10-01 的一次自动化文档调研）会把它们误判成未修。**

| 编号 | 结论 | 现状 | 修复 commit | 落点 |
|---|---|---|---|---|
| **H1** | 演示模式自锁死锁 | ✅ **已修** | `4a5a2f7` | `_FixtureProvider` 改 `threading.RLock()`（`__main__.py:217`）；`_RealtimeProvider` 仍用普通 `Lock`，但 `__main__.py:542` 有明确注释「刻意不引入任何重入路径」，24 处加锁全为叶子级、不嵌套 |
| **M1** | watchlist 写接口无鉴权 | ✅ **已修** | `4a5a2f7` → `b27a0a8` → `470737a` | `app.py:888-901` 要求 `Content-Type: application/json`，否则返 **415**；注释明确「不引入鉴权系统，仅抬高跨站触发门槛」 |
| **M2** | 空表 `AttributeError` | ✅ **已修** | `4a5a2f7` | `cpt/adapters/a_share_pool.py` |
| **L3 / L6** | — | ✅ **已修** | `4a5a2f7` | 同上 commit |
| **M3** | canvas iframe 信任边界（基线 S1） | ⬜ **仍开放** | — | 报告 §2 M3 |
| 低危 6 项 | 见 §4 | 🔶 部分处置 | — | 逐条看 §4 |

**仍然开放的两条**：

1. **M3 —— canvas iframe 信任边界**（`dashboard/canvas_d.js`）。本轮复核仍成立，
   未见修复 commit。
2. **低危组里的 fcntl 平台依赖** —— `a_share_pool.py` 顶层 `import fcntl` 让整个
   模块在 Windows 不可导入。这是**已知且刻意接受**的（见 `README.md`「质量门」的
   Windows 偏差说明），不算未修缺陷，但会让 Windows 上的全量测试永远差 13 条。

> 给后续读本文的人：**先看这张表再看正文**。正文保留原始证据链（文件:行号），
> 但行号可能已随修复漂移 —— 判断现状请以 `git log` 与实际代码为准。

---

## 0. 结论摘要（TL;DR）

| 级别 | 数量 | 要点 |
|---|---|---|
| 高 | 1 | 演示模式 Provider `select_symbol` 自锁死锁（**本次新发现**） |
| 中 | 3 | watchlist 写接口无鉴权；空表 `AttributeError`；canvas iframe 信任边界（基线 S1 仍成立） |
| 低 | 6 | f-string 常量 SQL、fcntl 平台依赖、watchlist 非原子写、缺安全响应头、宽泛 `except Exception`、flock 文档语义错误 |

**整体评价**：工程质量维持中上。密钥外置、SQL 参数化、危险函数零命中、工具链（mypy strict / ruff / import-linter / pre-commit）与 systemd 加固齐全。**唯一高危是演示模式提供者的锁设计缺陷**，生产 realtime 路径不受影响。

---

## 1. 高危

### H1 演示模式 Provider `select_symbol` 自锁死锁 — `cpt/web/__main__.py:213, 235-240, 319-325`

**证据链**：

- `:213` `self._lock = threading.Lock()` —— **不可重入**锁；
- `:235-240` `select_symbol()` 在 `with self._lock:` 块内调用 `self._build_snapshot()`；
- `:319-325` `_build_snapshot()` 函数体内**再次** `with self._lock:`；
- `:320` 注释自称「调用者负责持锁（`__init__` / `select_symbol` 内）」——与函数体仍取锁直接矛盾；且 `__init__`（`:218`）实际**并未持锁**，注释与两处调用点的真实行为都不一致。

**触发与影响**：`threading.Lock` 不可重入，同线程第二次 acquire 永久阻塞。演示模式（合成数据 Provider，非 realtime 模式）下任何一次 symbol/interval 切换都会：

1. 挂死当前 HTTP handler 线程；
2. 锁永不释放 → 后续所有 `snapshot_payload()`（`:242-244`）等取锁路径全部阻塞 → **整个服务失去响应**，只能杀进程。

**不受影响**：`_RealtimeProvider.select_symbol`（`:478-493`）只在锁内做字段赋值、不在锁内重建快照（由后台线程发布），生产 realtime 路径无此问题。

**修复建议**（任一即可）：

1. `threading.Lock()` → `threading.RLock()`（最小改动）；
2. 删除 `_build_snapshot` 内层 `with self._lock:`，把锁契约完全交给调用者（需先修正 `:320` 注释与 `__init__` 实际行为不符的问题）；
3. 拆分为 `_build_snapshot_locked()`（持锁前提）+ 无锁公开包装。

另建议补一条并发回归测试：`select_symbol` 调用后服务仍可正常响应快照请求。

---

## 2. 中危

### M1 watchlist 写接口无鉴权 — `cpt/web/app.py` + `cpt/web/a_share_routes.py:319-333`

- `watchlist_add` / `watchlist_remove`（`a_share_routes.py:319-333`）经 HTTP POST/DELETE 暴露，服务端与 nginx 模板中**均无任何鉴权、Origin 校验**；响应头也只有 `Content-Type` / `Content-Length`（`app.py:398-403`）。
- 缓解因素：服务绑定 `127.0.0.1:8010`（见 `deploy/env/cpt-dashboard.env.example`），直接攻击面取决于反代。但 `deploy/nginx/cpt-dashboard.conf` 把 `/cpt/api/` 反代到该端口，且线上已并入监听 443 的 `dsh-web` vhost —— **任何能访问站点的人都能读写全局唯一的一份自选清单**（单用户设计，见 `a_share_pool.py:17`）。
- 影响限于自选清单被篡改/清空（低价值目标、无横向移动面），故列中危而非高危。
- 建议：① nginx 侧对写方法加 `allow/deny` 或 `auth_request`；② 或服务侧加共享 token 头校验；③ 至少校验 `Content-Type: application/json`，提高跨站触发的门槛；④ 若未来引入 cookie 鉴权，需同步考虑 CSRF。

### M2 空表导致 `AttributeError` — `cpt/adapters/a_share_pool.py:130`

```python
cur.execute("SELECT max(date) FROM public.limit_pool_em")
trade_date = cur.fetchone()[0].isoformat()
```

`limit_pool_em` 为空表时 `max(date)` 返回 NULL → `(None,)` → `None.isoformat()` 抛 `AttributeError`。同文件 `:76 / :84` 的 `fetchone()[0]` 取 `max(date)` 模式相同，但那两处的 `None` 仅作为 `WHERE date = %s` 参数传入、查不到行即跳过，不崩；只有 `:130` 在结果上直接调方法。

**影响**：首次回填数据之前的正常运维窗口内，pool 接口报错。所幸上游 `pool_payload()` 按来源独立降级（`factor_error` / `strategy_error` / `watchlist_error` / `db_error` 字段，见 `a_share_routes.py:225-288`），爆炸半径被兜住、页面不白屏，但会以晦涩错误字符串呈现。

**建议**：`row = cur.fetchone()` 后判空，`row and row[0]` 为假时返回干净空态，而不是让异常穿透到降级层。

### M3 canvas iframe 信任边界（基线 S1，本次复核仍成立）

- `dashboard/canvas_d.js:152` `doc.body.innerHTML = payload.body_html`，iframe `sandbox="allow-same-origin allow-scripts"` **双授权下沙箱可被 iframe 内脚本自行移除**（MDN 明示），且 `runScripts()` 会重建并执行片段中的 `<script>`。
- 本次复核新增证据：服务端 `cpt/application/canvas_wbt.py` 对 `body_html` **不做转义/白名单**（全文无 `escape` / `sanitize`），内容可信完全依赖 wbt `HtmlReportBuilder` 的内部实现。
- 值得肯定：`:420-437` 有 CDN 引用泄漏检测（`_CDN_RE`），模板一旦引入外链即响亮失败。
- 建议维持基线结论：① 给 iframe 文档加 `Content-Security-Policy`；② 或对 `body_html` 做白名单 sanitize；③ 至少在代码注释中固化信任边界（「body_html 必须来自同源服务端可信模板」），防未来接入第三方数据源时埋雷。

---

## 3. 低危 / 加固建议

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| L1 | `a_share_local.py:119` 等 | f-string 拼接表名/列名。基线已记录：均为模块级常量、值全部走 `%s` 参数化，当前**无注入风险**，本次复核一致 | 模式本身脆弱——未来有人把变量接进 f-string 即破防。建议在这几处上方加注释强调「仅限模块级常量，禁止拼接变量」 |
| L2 | `a_share_pool.py:22` | 顶层 `import fcntl` → 该模块在 Windows 上**不可导入** | 非目标平台可忽略；如需跨平台，把 import 移入函数体内并对不可用平台降级 |
| L3 | `a_share_pool.py:221-223, 240-242` | watchlist 落盘为 `seek(0) + truncate() + json.dump`，**非原子**：写途中进程被杀 → 文件损坏 → 下一次 `add()` 把 `JSONDecodeError` 当空表处理（`:206-209`），**静默清空全部自选** | 写临时文件 + `os.replace()` 原子替换；或检测到损坏时先备份副本再重置 |
| L4 | `app.py:398-403` / nginx 模板 | 响应无安全头（`X-Content-Type-Options: nosniff` 等） | JSON API 实际风险低；服务端加一行 `nosniff`，或在 nginx 层统一加安全头 |
| L5 | 全仓 45 处 `except Exception` | 集中在 `a_share_snapshot.py`(15)、`__main__.py`(11)、`app.py`(6)、`a_share_routes.py`(4)。抽查均有注释说明降级理由，属「任何上游抖动不该让主视图不可用」的看板设计取舍 | 整体可接受；建议关键路径（watchlist 读写、DB 查询）收窄为具体异常类型——M2 的 `AttributeError` 就是被宽捕获掩盖成了错误字符串 |
| L6 | `a_share_pool.py:16-17, 170-173` | 文档称 fcntl 锁为「进程内锁，**单进程安全**」——与 flock 实际语义不符：flock 是同机**跨进程**有效的劝告锁（各进程独立 open 的 FD 正常互斥），只是不跨机/NFS | 修订注释，避免误导后续维护者做不必要的改造 |

另：基线 S2（`wind_source.py:125` 外部 Wind CLI，建议补 stderr 长度上限与二进制绝对路径白名单防 PATH 劫持）本次复核**仍成立**，代码未改动。

---

## 4. 做得好的地方（值得保持）

1. **密钥与配置外置**：`~/.dbconfig`、`~/.wind-aifinmarket`，全仓零硬编码密钥；env 模板只含非敏感项，还记录了「8010 端口被占用、不要用 8000」这类实战坑。
2. **危险函数零命中**：全 `cpt/` 无 `pickle.loads` / `yaml.load(` / `os.system` / `shell=True` / `verify=False` / `eval(` / `exec(`。
3. **domain 层**：纯函数 + `@dataclass(frozen, slots)` + `__post_init__` 值域校验（CanonicalBar 查 NaN/inf、OHLC 关系、时间合法性；RulesConfig 版本防绕过），docstring 记录设计依据与历史踩坑。
4. **部署侧已加固**：systemd `NoNewPrivileges=true` / `PrivateTmp=true` / `Restart=on-failure`；nginx 模板与线上实况对齐并明确警告「不要 cp 进 sites-enabled」；0929 审计指出的「nohup 无自动重启」已整改为 systemd 托管。
5. **降级架构**：pool 接口按来源独立降级（factor / strategy / watchlist / db 各自 error 字段），单点上游故障不拖垮主视图。
6. **canvas_wbt 的 CDN 泄漏防线**：模板一旦引入外链即响亮失败，离线验收纪律落到了代码里。
7. **watchlist 实现总体扎实**：fcntl 锁 + 幂等 add + 文件缺失时自动初始化（`__init__` 写 `"[]"`），本次确认无「文件不存在即崩溃」问题。

---

## 5. 与基线（2026-09-25）对照

| 基线条目 | 本次复核状态 |
|---|---|
| S1 canvas iframe（`canvas_d.js:152`） | **仍成立** → 本报告 M3，补充服务端无转义的新证据 |
| S2 `wind_source.py` CLI 加固建议 | 仍成立，未改动 |
| S3 localStorage noteKey 命名空间 | 本轮未复核，以基线为准 |
| S4 `a_share_snapshot.py` 可变全局 `_FETCHER` | 本轮未复核，以基线为准 |
| 复杂度热点（`app.py:make_handler` CC94 / `dashboard.js` 3186 行） | 本轮未重测，以基线为准 |
| 死模块（`engine/`、`storage/`、14 个 `dashboard_*`） | 本轮未重测，以基线为准 |
| **H1 演示模式自锁死锁** | **本次新发现** |

---

## 6. 覆盖范围与诚实声明

- **本轮逐行/逐段覆盖**：`cpt/web/`（app.py、`__main__.py` 关键路径、a_share_routes.py）、`cpt/adapters/`（a_share_pool.py、a_share_local.py 关键段）、`cpt/domain/`（全层）、`dashboard/`（canvas_d.js 关键注入点 + 全目录 innerHTML 面扫描）、`deploy/`（nginx / env / systemd 全文）、既有 5 份审计文档比对。
- **以基线为准、本轮未重测**：`cpt/application/` 大部（仅 canvas_wbt 与 grep 面）、`scripts/factor_backfill.py`、`tests/`、`dashboard/dashboard.js` 主体。
- 所有结论均可追溯到上文标注的 `文件:行号` 证据；未复核区域不做结论。
