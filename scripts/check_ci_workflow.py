#!/usr/bin/env python3
"""门禁⑩：CI workflow 的 YAML **缩进**不能把 step 吞进 `run: |` 块标量。

## 为什么要这道门（R51 实测踩出来的）

2026-10-05 R51 推送后 CI 仍红。本地全绿、pytest 在 CI 上也绿
（`1051 passed`），所有门禁的输出都打了 ✅，最后一步却：

    line 33: -: command not found
    ##[error]Process completed with exit code 127.

真因不在脚本，在 `.github/workflows/ci.yml` 本身：

    - name: Static quality gates
      run: |
        python scripts/check_doc_drift.py
        - name: Dashboard bundle in sync        # ← 缩进 10 空格 = 仍在 run 文本内
          run: python scripts/build_dashboard_bundle.py --check
        - name: Doc claims (full sweep)         # ← 同上
          ...

YAML 的 `run: |` 是**块标量**：其后所有比 `run:` 键缩进更深的行都是字符串内容。
于是这 5 个 step 变成了 shell 脚本里的 5 行文本，bash 把 `- name: ...` 当命令
执行 ⇒ `-: command not found` ⇒ 整个 `Static quality gates` 步骤 exit 127。

**后果比"红"严重得多**：这 5 个门禁
（`build_dashboard_bundle --check` / `check_all_claims` / `check_doc_counts` /
`check_enqueue_skeleton_unique` / `check_job_poll_unique`）在 CI 上
**从来没有真正跑过一次**，而整条链又恒红，所以没人看见。

该缩进错误由 `1cda27af1`（R45 dashboard 拆分）引入，此后一直被 pytest 的
红灯挡在前面、从未暴露 —— R51 删掉画布 D 让 pytest 转绿，它才浮出来。

⇒ 判据：扫 `.github/workflows/*.yml`，把每个 `run: |` / `run: >` 块标量的
**实际内容**抠出来，若内容里出现 `- ` 开头的行（step）或 `run:`（step 的字段），
判定为「step 被吞」，退出码 1。

## 为什么不用 PyYAML

CI 环境装了 pyyaml，但本仓的 `.venv` 没有；门禁不该依赖一个装不装得上的包，
也不该为了装包去动 requirements。块标量的缩进规则在 YAML 规范里就一句话
（「比父键更深」），手写十行足够，且**不需要真正解析 YAML**。

## 用法

    python scripts/check_ci_workflow.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WF_DIR = ROOT / ".github" / "workflows"

#: ``run: |`` / ``run: >``（可带 chomping/缩进指示符，如 ``|-`` ``>-`` ``|2-``）
_BLOCK_SCALAR = re.compile(r"^(?P<indent>\s*)run:\s*[|>][-+0-9]*\s*(#.*)?$")
#: 块标量**内容里**出现这些行 ⇒ 有 step 被吞进去了
_STEP_LINE = re.compile(r"^\s*(?:-\s*name:|-\s*uses:|-\s*run:|run:)")


def _scan(path: Path) -> list[str]:
    """返回该文件里所有「step 被吞进 run 块标量」的描述。"""
    problems: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        m = _BLOCK_SCALAR.match(lines[i])
        if not m:
            i += 1
            continue
        key_indent = len(m.group("indent"))
        i += 1
        # 收集块标量内容：缩进严格大于 ``run:`` 键缩进的连续行
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            cur = len(line) - len(line.lstrip())
            if cur <= key_indent:
                break  # 回到 step 层级 ⇒ 块标量结束
            if _STEP_LINE.match(line):
                problems.append(
                    f"{path.relative_to(ROOT)}:{i + 1}: step 被吞进上一条 `run: |` 块标量 "
                    f"（缩进 {cur} > run 键的 {key_indent}）⇒ bash 会把 "
                    f"{line.strip()[:40]!r} 当命令执行，报 `command not found`\n"
                    f"    {line.rstrip()}"
                )
            i += 1
    return problems


def main() -> int:
    if not WF_DIR.is_dir():
        print(f"  ⚠️ {WF_DIR.relative_to(ROOT)} 不存在，本门禁无对象可查")
        return 0
    files = sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml"))
    if not files:
        print("  ⚠️ .github/workflows/ 下没有 yml，本门禁无对象可查")
        return 0

    problems: list[str] = []
    for f in files:
        problems.extend(_scan(f))

    if problems:
        print("❌ CI workflow 缩进坏了：")
        for p in problems:
            print(f"  {p}")
        print(
            "\n  ⇒ 这些 step 从未在 CI 上执行过（整条 `run:` 步骤会恒定 exit 127）。\n"
            "    修法：把它们的 `- name:` 缩进**提到与 `- name:` 同级**"
            "（即与 `run:` 键对齐），而不是留在 `run: |` 的内容缩进里。"
        )
        return 1

    print(f"  ✅ workflow 缩进正常（{len(files)} 个文件，step 未被吞进 run 块标量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
