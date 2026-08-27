#!/usr/bin/env bash
# ci-local.sh — 本地模擬 GitHub CI（push 前自己驗證）
# 同 .github/workflows/ci.yml 做嘅步驟一樣：
#   ShellCheck → bash -n → py_compile → BATS
# 用法：bash ci-local.sh（或者 make check）
# 2026-08-27 建立（里程碑 10：CI/CD 完整化）
set -u

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
FAIL=0

step() { echo "==> $*"; }
ok()   { echo "   ✅ $*"; }
bad()  { echo "   ❌ $*"; FAIL=1; }

step "ShellCheck（靜態分析）"
if shellcheck scripts/*.sh install.sh; then
  ok "ShellCheck"
else
  bad "ShellCheck"
fi

step "Syntax check（bash -n）"
if bash -n scripts/*.sh install.sh; then
  ok "bash -n"
else
  bad "bash -n"
fi

step "Python compile check"
if python3 -m py_compile scripts/share-queue.py scripts/semantic-patrol.py scripts/topic-factory.py; then
  ok "py_compile"
else
  bad "py_compile"
fi

step "BATS 測試"
if command -v bats >/dev/null 2>&1; then
  BATS="bats"
elif [ -x /tmp/bats-core/bin/bats ]; then
  BATS="/tmp/bats-core/bin/bats"
else
  echo "   ⚠️ 搵唔到 bats——請裝 bats-core（https://github.com/bats-core/bats-core）"
  echo "   ⚠️ CI 會自動裝，本地可以：git clone --depth 1 https://github.com/bats-core/bats-core /tmp/bats-core"
  bad "bats 未安裝"
  exit 1
fi
if "$BATS" tests/; then
  ok "BATS"
else
  bad "BATS"
fi

echo
if [ "$FAIL" = "0" ]; then
  echo "🎉 全部檢查通過（同 GitHub CI 一樣）"
  exit 0
else
  echo "❌ 有檢查失敗——修好先好 push"
  exit 1
fi
