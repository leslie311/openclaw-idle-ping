#!/usr/bin/env bash
# sync-verify.sh — 本地 workspace ↔ 開源 repo 完整性驗證
# 2026-09-01 建立（Leslie 要求：點樣確定擺上 GitHub/Skill 嘅版本同本地一樣）
#
# 原理：
#   1. 逐個檔案比較「本地版」同「repo 版」
#   2. 比較前先 normalize（統一已知 sanitize 差異：Telegram ID、用戶名路徑 → placeholder）
#   3. 一致 → PASS；唔一致 → 顯示 diff + FAIL
#
# 用法：
#   bash scripts/sync-verify.sh           # 驗證（預設：normalize 後比較）
#   bash scripts/sync-verify.sh --strict  # 完全一致先 PASS（唔做 normalize）
#
# 退出碼：0 = 全部一致；1 = 有差異
#
# 背景（2026-09-01）：
#   sync 流程（cp → 檢查 → commit）一直冇自動 hash 驗證——Leslie 問「點確定擺上去
#   同本地一樣」先發現。呢個 script 就係補返嗰一步：每次 push 前跑一次，除咗
#   sanitize allowlist（公開 repo 唔可以有真 Telegram ID 等私隱）之外全部要一致。
set -u

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_SCRIPTS="${SYNC_VERIFY_WS_SCRIPTS:-$HOME/.openclaw/workspace/scripts}"
REPO_SCRIPTS="${SYNC_VERIFY_REPO_SCRIPTS:-$HOME/.openclaw/workspace/share/idle-ping/scripts}"

# 要對比嘅檔案（相對路徑）——有新 script 記得加
FILES="idle-ping-gate.sh idle-ping-note.py test-idle-ping.sh"

# normalize：將已知 sanitize 差異統一做 placeholder（兩邊都做相同 substitution，
# 先唔會誤報——即係「公開版同本地版只差私隱位」都算一致）
normalize() {
  sed -E \
    -e 's/telegram:direct:[0-9]+/telegram:direct:<ID>/g' \
    -e 's|/home/[a-zA-Z0-9_]+|~|g' \
    "$1"
}

FAIL=0
for f in $FILES; do
  if [ ! -f "$WS_SCRIPTS/$f" ]; then
    echo "❌ $f：本地唔存在（$WS_SCRIPTS/$f）"
    FAIL=1
    continue
  fi
  if [ ! -f "$REPO_SCRIPTS/$f" ]; then
    echo "❌ $f：repo 唔存在（$REPO_SCRIPTS/$f）"
    FAIL=1
    continue
  fi

  if [ "$STRICT" = "1" ]; then
    if cmp -s "$WS_SCRIPTS/$f" "$REPO_SCRIPTS/$f"; then
      echo "✅ $f 一致（strict）"
    else
      echo "❌ $f 唔同（strict）——下面係 diff（< 本地 / > repo）："
      diff "$WS_SCRIPTS/$f" "$REPO_SCRIPTS/$f" | head -10
      FAIL=1
    fi
  else
    if diff <(normalize "$WS_SCRIPTS/$f") <(normalize "$REPO_SCRIPTS/$f") > /dev/null 2>&1; then
      echo "✅ $f 一致（normalize 後）"
    else
      echo "❌ $f 唔同（normalize 後仍唔一致）——下面係 diff（< 本地 / > repo）："
      diff <(normalize "$WS_SCRIPTS/$f") <(normalize "$REPO_SCRIPTS/$f") | head -10
      FAIL=1
    fi
  fi
done

if [ "$FAIL" = "0" ]; then
  echo "🎉 全部一致"
else
  echo "⚠️ 有 $FAIL 個檔案唔一致"
fi
exit "$FAIL"
