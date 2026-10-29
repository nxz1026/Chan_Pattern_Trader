"""门禁⑰ · 解释器能不能从任意目录导入本仓（R74）。

## 为什么需要这道门禁

2026-10-20 起，`cpt-dashboard-ashare.service` 与 `watchlist-daily-summary` cron
连续两天每天失败，日志都是同一句：

    ModuleNotFoundError: No module named 'cpt'

根因不是脚本写错了，是 **`.venv` 里的 editable 安装指向了已删除的路径**：

    MAPPING = {'cpt': '/home/ubuntu/work/.morning/Chan_Pattern_Trader/cpt'}

这个目录在 2026-10-14 之后就不存在了。它之所以两周没被发现，是因为：

1. 开发时跑测试一律写 `PYTHONPATH=.`，这条路绕开了 editable 安装；
2. `python -m cpt.web` 会把 **CWD** 放进 ``sys.path[0]``，CWD 恰好是仓根；
3. cron 与 systemd 直接 `python /path/to/scripts/x.py`，此时 ``sys.path[0]``
   是 **脚本所在目录**（`scripts/`），CWD 根本不参与 —— 于是只有它们炸。

一句话：**所有带 `PYTHONPATH=` 的验证都绕开了真实故障路径。**
所以这道门禁必须在**不带 `PYTHONPATH`、且 CWD 在仓外**的条件下验证。

## 判据

从仓库外的临时目录、用仓内解释器、清空 ``PYTHONPATH`` 跑一句
``import cpt`` 并打印 ``cpt.__file__``：

- 必须成功；
- 落点必须在本仓内（防止它导入了**另一份**旧 clone —— 这正是本 bug 的形态：
  导入本身没报错，只是导进了别处）。

## 为什么不再用「文件里 grep 路径」

本 bug 的文件形态只需改一行字符串就能骗过 grep，而真实判据是
「这个解释器到底能不能 import 到本仓」。用子进程实测比读文件可靠。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("CPT_REPO") or Path(__file__).resolve().parents[1])


def _resolve_python() -> Path:
    """探针解释器：``CPT_PYTHON`` → 仓内 ``.venv`` → **当前解释器**。

    最后一档是给 CI 用的：runner 上没有 ``.venv``，早先硬编码
    ``ROOT/.venv/bin/python`` 让本门禁在 CI 上**结构性必红**
    （2026-10-29 审计实测）。用 ``sys.executable`` 仍然验证「仓外目录、
    无 PYTHONPATH 时能不能 import cpt」这件事本身，只是换了跑它的解释器。
    """
    explicit = os.environ.get("CPT_PYTHON")
    if explicit:
        return Path(explicit)
    venv_python = ROOT / ".venv" / "bin" / "python"
    return venv_python if venv_python.exists() else Path(sys.executable)


PYTHON = _resolve_python()

MARK = "✗"
OK = "✓"


def _runner() -> tuple[bool, str, str | None]:
    """在仓外目录、无 PYTHONPATH 的条件下 import cpt。

    返回 ``(成功, 落点或错误, 失败时的 stderr 尾巴)``。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import json, importlib.util;"
        "spec = importlib.util.find_spec('cpt');"
        "print(json.dumps({'file': getattr(spec, 'origin', None)})"
        " if spec else 'null')"
    )
    with tempfile.TemporaryDirectory(prefix="cpt-import-probe-") as outside:
        proc = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=outside,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return False, "import cpt 失败", "\n".join(tail)
    raw = (proc.stdout or "").strip()
    if raw == "null":
        return False, "import cpt 失败（模块根本没找到）", None
    try:
        origin = json.loads(raw).get("file")
    except json.JSONDecodeError:
        return False, f"探针输出无法解析：{raw[:120]}", None
    if not origin:
        return False, "find_spec('cpt') 没有 origin", None
    return True, origin, None


def main() -> int:
    print("📋 门禁⑰ · 解释器从任意目录都能导入本仓")

    problems: list[str] = []

    if not PYTHON.exists():
        print(f"  {MARK} 解释器不存在：{PYTHON}")
        print("❌ 有违规")
        return 1
    print(f"  {OK} 解释器 {PYTHON}")

    ok, detail, stderr_tail = _runner()
    if not ok:
        print(f"  {MARK} 仓外目录、无 PYTHONPATH 时 {detail}")
        if stderr_tail:
            for line in stderr_tail.splitlines():
                print(f"      {line}")
        print("      ⇒ 这是 cron / systemd 的真实运行条件。修法：")
        print(f"        {PYTHON} -m pip install -e {ROOT} --no-deps")
        problems.append("仓外不能 import cpt")
    else:
        origin = Path(detail).resolve()
        try:
            origin.relative_to(ROOT.resolve())
        except ValueError:
            print(f"  {MARK} 导入落点在本仓之外：{origin}")
            print("      ⇒ editable 安装指向了别的 clone，改代码不会生效。")
            problems.append("导入落点不在本仓")
        else:
            print(f"  {OK} 仓外导入成功，落点 {origin}")

    if problems:
        print(f"❌ 有违规（{len(problems)} 项）")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
