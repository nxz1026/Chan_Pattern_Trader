#!/usr/bin/env bash
# 一键拉取并固定 CPT 的两个参考仓库到 references/ 子目录。
#
# 用法: scripts/fetch_references.sh
#
# 行为:
#   - 目录不存在 -> git clone --no-checkout 后 checkout 到固定 commit
#   - 目录已存在 -> git fetch + git checkout <commit>
#   - 校验工作区无未提交改动(若脏,HEAD commit 校验不可信,直接失败)
#   - 输出每个仓库实际 HEAD hash,与期望值校验;任一不一致则退出码非 0
#   - 打印每个仓库的 LICENSE 路径(若存在)
#
# 约束: bash 4+;不使用 git submodule;失败时给出明确仓库名 + commit。
# 所有 git 命令保留 stderr 输出,失败原因不被吞掉。

set -euo pipefail

# 仓库定义: 名称|仓库 URL|期望 commit|许可证(SPDX)
REPOS=(
  "czsc|https://github.com/waditu/czsc.git|701e480a545004f945bb1721e510ae610ad90c4c|Apache-2.0"
  "wbt|https://github.com/zengbin93/wbt.git|39bb1e8ab7db71cce2dcea24150639e9470a4ed4|MIT"
)

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname -- "$SCRIPT_DIR")"
REFS_DIR="$ROOT_DIR/references"

fail() {
  echo "错误[$1]: $2" >&2
  exit 1
}

[ "${BASH_VERSINFO[0]}" -ge 4 ] || fail "bash" "需要 bash 4+,当前 ${BASH_VERSION}"

mkdir -p "$REFS_DIR"

declare -a FAILURES=()

for entry in "${REPOS[@]}"; do
  IFS='|' read -r name url commit license <<< "$entry"

  target="$REFS_DIR/$name"
  echo "==> [$name] 目标 commit: $commit"

  if [ -d "$target/.git" ]; then
    echo "    目录已存在,执行 git fetch..."
    # 若工作区有未提交改动,HEAD 校验不可信:直接 fail
    if ! git -C "$target" diff --quiet HEAD --; then
      fail "$name" "工作区存在未提交改动,commit 校验不可信;请先 git stash 或 git checkout ."
    fi
    git -C "$target" fetch --tags --force origin \
      || fail "$name" "git fetch 失败 (origin: $url)"
  else
    echo "    目录不存在,git clone --no-checkout..."
    git clone --no-checkout "$url" "$target" \
      || fail "$name" "git clone 失败 (url: $url)"
  fi

  git -C "$target" checkout "$commit" \
    || fail "$name" "checkout 失败 (commit: $commit)"

  actual="$(git -C "$target" rev-parse HEAD)"

  if [ "$actual" = "$commit" ]; then
    echo "    HEAD: $actual   [OK]"
  else
    echo "    HEAD: $actual   [不符!] 期望: $commit" >&2
    FAILURES+=("$name: 期望 $commit,实际 $actual")
  fi

  # 打印 LICENSE 路径(大小写不敏感,若存在)
  license_path="$(find "$target" -maxdepth 1 -iname 'licen[cs]e*' -type f -print -quit)"
  if [ -n "$license_path" ]; then
    echo "    LICENSE: ${license_path#$REFS_DIR/}"
  else
    echo "    LICENSE: (未找到)"
  fi

  echo ""
done

if [ "${#FAILURES[@]}" -gt 0 ]; then
  echo "校验失败:" >&2
  for f in "${FAILURES[@]}"; do
    echo "  - $f" >&2
  done
  exit 1
fi

echo "全部参考仓库已就位且 commit 校验通过。"
exit 0
