#!/usr/bin/env bash
# R45 P0-1：出一次覆盖率 + 「生产路径上没测透的函数」清单。
#
# ⚠️ **只报告，不阻断** —— 门槛要先有基线，否则第一次跑就红。
#    接进 CI 之前，先把本地产出的 P0 清单清到可接受规模。
#
# 依赖：coverage（R45 在 Oracle venv 里手工装的一次，正式用应进
#       requirements-dev.txt —— 那一步需要 owner 同意，因为会给 CI 加一个包）。
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python}"
"$PY" -m coverage run --source=cpt -m pytest tests/ -q --no-header \
    -p no:cacheprovider --tb=no || true
"$PY" -m coverage json -o /tmp/coverage.json >/dev/null
"$PY" scripts/report_coverage_gaps.py
