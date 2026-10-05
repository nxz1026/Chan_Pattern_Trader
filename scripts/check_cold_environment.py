#!/usr/bin/env python3
"""门禁⑬：测试的结论**不许是本机环境的函数** —— 把环境抽掉重跑。

## 为什么要这道门（R49 实测踩出来的）

R49 全量复验时冒出 2 个「本机红、CI 绿」（或反之）的失败。当时的初判是
「环境依赖，与本轮无关」，**这个定性是错的**（R50 推翻）——两个用例的真实根因
都是**测试自身的判据缺陷**：把「本机恰好没有某物」当成了前提。

R49 的两个样本：

    test_dashboard_url_credentials.py   沙箱里没注入 url_safety.js，
                                        失败被误读成「node v22 的 URL 解析差异」
    test_factor_backfill_script.py      判据把「本机没装 Wind」当前提，
                                        于是拿到 wind_basis_unknown 而非 wind_unavailable

共同点：**用例的胜负取决于跑它的那台机器**。这不会产生任何一处红色的代码，
只会让人在「换个环境好了/坏了」里反复打转。

## 为什么不用静态判据（两条路都实测否决了）

1. **名字启发式**（名字带 missing/unavailable/degrade ⇒ 必须有 patch）：
   实测命中 **99 个**用例，其中 61 个看不到 patch —— 但逐条看，它们说的「缺」
   是**数据缺**（缺一天、缺一列、缺一个字段），由用例自己构造，**与环境无关**。
   假阳性高到会让门禁被无视。
2. **环境探测启发式**（出现 `shutil.which` / `Path.home` / `os.environ` ⇒ 必须有
   skip 守卫）：实测只有 7 个模块探环境，其中 5 个已有守卫，余下 2 个是
   `subprocess.Popen` 启动自家服务，属正当。**今日零违规 ⇒ 门禁立起来只是摆设**；
   更要命的是它**抓不到 R49 的真身** —— 那例代码里根本没有任何探测调用，
   靠的是「本机恰好没装 Wind」这个沉默事实。

⇒ ② 的真身是**执行级**的，不是文本级的。**判据只能是「换个环境再跑一遍」。**

## 判据

以 `PATH=/nonexistent`、`HOME=<空目录>`（其余环境原样保留）重跑整个 `tests/`：

  · 有 failed / error ⇒ 该用例的结论是本机环境的函数 ⇒ **判失败**；
  · skip **不算失败**（「本机没有这个能力」是 skip 的正当用途），
    但必须**逐条打出来** —— R49 的另一条教训是「绿灯来自没执行」比红更糟。

## 用法

    python scripts/check_cold_environment.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 冷环境跑 pytest 的上限（秒）。实测约 75 秒量级，留足余量。
TIMEOUT = 900

#: 抽掉 PATH 时给一个**必然不存在**的值（不能用空串：空串有时被当成 "."）
COLD_PATH = "/nonexistent"

#: 临时 HOME 建在工作区里，**不放 /tmp** —— 本仓 /tmp 配额紧且不支持 SQLite WAL
SCRATCH = ROOT / ".pytest_cache"


def _run_cold(home: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = COLD_PATH
    env["HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "-r",
            "s",  # 把 skip 的理由列出来
        ],
        # ⚠️ 别再给 `-q`：pyproject 的 addopts 已有 `-q`，加到 `-qq` 会把
        # 「N passed」汇总行吃掉、只剩点阵（R49 踩过）。
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def main() -> int:
    tests = ROOT / "tests"
    if not tests.is_dir():
        print(f"  ⚠️ {tests.relative_to(ROOT)} 不存在，本门禁无对象可查")
        return 0
    if shutil.which("pytest", path=COLD_PATH) is not None:  # pragma: no cover - 自证
        print(f"❌ 冷环境的 PATH 竟然能找到 pytest（{COLD_PATH}）—— 本门禁失效")
        return 1
    SCRATCH.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="coldhome-", dir=str(SCRATCH)))
    try:
        out = _run_cold(tmp_root)
    except subprocess.TimeoutExpired:
        print(f"❌ 冷环境重跑超过 {TIMEOUT} 秒仍未结束")
        return 1
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    blob = out.stdout + out.stderr
    lines = blob.splitlines()
    skipped = [ln.strip() for ln in lines if ln.strip().startswith("SKIPPED")]
    failed = [ln for ln in lines if ln.startswith("FAILED") or ln.startswith("ERROR")]
    summary = next((ln for ln in reversed(lines) if re.search(r"\d+ (passed|failed)", ln)), "")

    if out.returncode != 0:
        print("❌ 把本机环境抽掉之后，测试的结论就变了 —— 这些用例依赖本机环境：")
        for ln in failed[:20]:
            print(f"  {ln}")
        if len(failed) > 20:
            print(f"  … 另 {len(failed) - 20} 条")
        for ln in lines[-25:]:
            print(f"  │ {ln}")
        print(
            "\n  ⇒ 一个用例的胜负不该取决于跑它的那台机器（R49 就是这样连着红了三轮）。\n"
            "    修法：让用例**自己控制**它依赖的条件（注入 / monkeypatch / 夹具），\n"
            "    而不是假设「本机恰好没有某物」；确实需要本机能力的，用 "
            "`pytest.skip` 明确跳掉并写明理由。"
        )
        return 1

    print(
        f"  ✅ 抽掉 PATH/HOME 之后结论不变（PATH={COLD_PATH}、空 HOME）；{summary or '汇总行缺失'}"
    )
    for ln in skipped:
        print(f"  · {ln}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
