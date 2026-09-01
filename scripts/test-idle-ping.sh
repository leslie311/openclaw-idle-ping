#!/usr/bin/env bash
# test-idle-ping.sh — idle-ping gate + note 邏輯 sandbox 測試（2026-09-01 建立）
#
# 背景：2026-09-01 修復「出咗 4 次貨先暫停」bug（gateStreak 倒退偵測 + 簿記機械化）。
# 呢個 script 用 sandbox（/tmp）模擬 gate script 所有分支，驗證行為正確，唔掂真 state。
#
# 用法：bash scripts/test-idle-ping.sh
# 依賴：scripts/idle-ping-gate.sh、scripts/system-log.sh、scripts/idle-ping-note.py
set -u

PASS=0
FAIL=0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE_SRC="$SRC_DIR/idle-ping-gate.sh"
LOG_SRC="$SRC_DIR/system-log.sh"
NOTE_SRC="$SRC_DIR/idle-ping-note.py"

# --- sandbox setup ---
SB="$(mktemp -d /tmp/ip-sandbox.XXXXXX)"
trap 'rm -rf "$SB"' EXIT
mkdir -p "$SB/scripts" "$SB/curiosity"
cp "$GATE_SRC" "$LOG_SRC" "$SB/scripts/"
GATE="$SB/scripts/idle-ping-gate.sh"

# 預設 sandbox idle-ping.env（各 case 可覆寫）
write_env() {
  cat > "$SB/scripts/idle-ping.env" <<EOF
IDLE_PING_WS=$SB
IDLE_PING_SESSIONS_JSON=$SB/sessions.json
IDLE_PING_LOG=$SB/system-activity.log
IDLE_PING_SEND_JOB_ID=
IDLE_PING_PAUSE_JOB_ID=test-pause
EOF
}

# 寫 state.json：$1 = python dict literal（可用 now / now_ms 變數）
write_state() {
  python3 - "$SB/curiosity/state.json" "$1" <<'PY'
import json, sys, time
p, expr = sys.argv[1], sys.argv[2]
now = int(time.time() * 1000)
s = eval(expr, {"__builtins__": {}}, {"now": now, "now_ms": now})
json.dump(s, open(p, "w"), ensure_ascii=False, indent=2)
PY
}

# 寫 sessions.json：$1 = lastInteractionAt（epoch ms）
write_sessions() {
  python3 - "$SB/sessions.json" "$1" <<'PY'
import json, sys
s = {
  "agent:main:telegram:direct:123456789": {"lastInteractionAt": int(sys.argv[2])},
}
json.dump(s, open(sys.argv[1], "w"), ensure_ascii=False)
PY
}

# 讀 state 某 key
state_get() {
  python3 - "$SB/curiosity/state.json" "$1" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print(s.get(sys.argv[2], ""))
PY
}

# 檢查 sandbox system log 有冇 substring
log_has() {
  grep -qF "$1" "$SB/system-activity.log" 2>/dev/null
}

check() {  # $1 = case 名, $2 = 0/1（1 = pass）
  if [ "$2" = "1" ]; then
    echo "PASS: $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $1"
    FAIL=$((FAIL + 1))
  fi
}

reset_log() {
  : > "$SB/system-activity.log"
}

NOW_MS="$(python3 -c 'import time; print(int(time.time()*1000))')"
H2_AGO=$((NOW_MS - 2 * 3600 * 1000))
H3_AGO=$((NOW_MS - 3 * 3600 * 1000))
H1_AGO=$((NOW_MS - 1 * 3600 * 1000))
MIN5_AGO=$((NOW_MS - 5 * 60 * 1000))

echo "=== idle-ping sandbox 測試 ==="

# ---------- Case 1: 正常中骰出貨（SHIP streak=1） ----------
write_env
write_sessions "$H3_AGO"
write_state "{'pingStreak': 0, 'gateStreak': 0, 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False}"
reset_log
IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
S=$(state_get pingStreak); G=$(state_get gateStreak)
check "Case1 正常中骰 → SHIP streak=1 (got $S)" "$([ "$S" = "1" ] && [ "$G" = "1" ] && echo 1 || echo 0)"

# ---------- Case 2: 連續 3 次中骰冇回應 → 第 3 次收骰仔（PAUSE） ----------
# 模擬：上次中骰後推返 2 小時前（避 cooldown），用戶一直冇覆
for i in 2 3; do
  write_sessions "$H3_AGO"
  write_state "{'pingStreak': $((i-1)), 'gateStreak': $((i-1)), 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False}"
  reset_log
  IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
  S=$(state_get pingStreak)
  if [ "$i" = "2" ]; then
    check "Case2a 第2次中骰 → SHIP streak=2 (got $S)" "$([ "$S" = "2" ] && echo 1 || echo 0)"
  else
    check "Case2b 第3次中骰 → PAUSE（pingStreak=3）(got $S)" "$([ "$S" = "3" ] && echo 1 || echo 0)"
    log_has "🛑 收骰仔" && check "Case2c PAUSE log 有 🛑 收骰仔" 1 || check "Case2c PAUSE log 有 🛑 收骰仔" 0
  fi
done

# ---------- Case 3: 用戶覆咗 → RESET 重開 ----------
write_sessions "$H1_AGO"   # 用戶 1 小時前覆咗（idle 夠 + last_msg > last_ship）
write_state "{'pingStreak': 3, 'gateStreak': 3, 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': $H2_AGO, 'pauseNoticeSent': True}"
reset_log
IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
S=$(state_get pingStreak)
check "Case3 暫停中用戶覆咗 → RESET 重開 + SHIP streak=1 (got $S)" "$([ "$S" = "1" ] && echo 1 || echo 0)"
log_has "✅ 暫停重開" && check "Case3b log 有 ✅ 暫停重開" 1 || check "Case3b log 有 ✅ 暫停重開" 0

# ---------- Case 4: 倒退偵測（agent 覆蓋 state 模擬） ----------
write_sessions "$H3_AGO"
# gateStreak=2 但 pingStreak=1 → 倒退（模擬 2026-09-01 事件）
write_state "{'pingStreak': 1, 'gateStreak': 2, 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False}"
reset_log
IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
S=$(state_get pingStreak); G=$(state_get gateStreak)
check "Case4 倒退偵測 → 照常 SHIP streak=2 (got $S/$G)" "$([ "$S" = "2" ] && [ "$G" = "2" ] && echo 1 || echo 0)"
log_has "⚠️ gate 偵測 pingStreak 倒退" && check "Case4b log 有 ⚠️ 倒退警告" 1 || check "Case4b log 有 ⚠️ 倒退警告" 0

# ---------- Case 5: Cooldown lock（30 分鐘內唔再中） ----------
write_sessions "$H3_AGO"
write_state "{'pingStreak': 1, 'gateStreak': 1, 'lastShipAt': $MIN5_AGO, 'lastPingTriggerAt': $MIN5_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False}"
reset_log
IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
S=$(state_get pingStreak)
check "Case5 cooldown 30min 內 → LOCKED 唔出貨 (streak 保持 $S)" "$([ "$S" = "1" ] && echo 1 || echo 0)"

# ---------- Case 6: 靜音窗口 ----------
write_env
echo "IDLE_PING_SILENT_START=0" >> "$SB/scripts/idle-ping.env"   # H>=0 永遠靜音
write_sessions "$H3_AGO"
write_state "{'pingStreak': 0, 'gateStreak': 0, 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False}"
reset_log
IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
S=$(state_get pingStreak)
check "Case6 靜音窗口 → 唔出貨 (streak 保持 $S)" "$([ "$S" = "0" ] && echo 1 || echo 0)"

# ---------- Case 7: idle 未夠 30 分鐘 ----------
write_env
write_sessions "$MIN5_AGO"   # 5 分鐘前有 message → idle 唔夠
write_state "{'pingStreak': 0, 'gateStreak': 0, 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False}"
reset_log
IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
S=$(state_get pingStreak)
check "Case7 idle <30min → 唔出貨 (streak 保持 $S)" "$([ "$S" = "0" ] && echo 1 || echo 0)"

# ---------- Case 8: 骰仔唔中 ----------
write_env
write_sessions "$H3_AGO"
write_state "{'pingStreak': 0, 'gateStreak': 0, 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False}"
reset_log
IDLE_PING_DICE_RANGE=100000 bash "$GATE" > /dev/null 2>&1   # 幾乎必唔中
S=$(state_get pingStreak)
check "Case8 骰仔唔中 → 唔出貨 (streak 保持 $S)" "$([ "$S" = "0" ] && echo 1 || echo 0)"

# ---------- Case 9: 每日上限 DAILY_CAP ----------
write_env
echo "IDLE_PING_DAILY_CAP=2" >> "$SB/scripts/idle-ping.env"
TODAY="$(TZ=Asia/Hong_Kong date +%Y-%m-%d)"
write_sessions "$H3_AGO"
write_state "{'pingStreak': 0, 'gateStreak': 0, 'lastShipAt': $H2_AGO, 'lastPingTriggerAt': $H2_AGO, 'lastPauseNoticeAt': 0, 'pauseNoticeSent': False, 'dailyCount': 2, 'dailyDate': '$TODAY'}"
reset_log
IDLE_PING_DICE_RANGE=1 bash "$GATE" > /dev/null 2>&1
S=$(state_get pingStreak)
check "Case9 每日上限已滿 → 唔出貨 (streak 保持 $S)" "$([ "$S" = "0" ] && echo 1 || echo 0)"

# ---------- Case 10: idle-ping-note.py 唔掂 gate 地盤 key ----------
write_state "{'pingStreak': 5, 'gateStreak': 5, 'lastShipAt': 12345, 'lastPingTriggerAt': 67890, 'lastPauseNoticeAt': 111, 'pauseNoticeSent': True, 'dailyCount': 3, 'dailyDate': '2026-09-01', 'currentTopic': '舊topic', 'lastTopics': ['舊topic']}"
OUT=$(IDLE_PING_STATE_JSON="$SB/curiosity/state.json" python3 "$NOTE_SRC" --topic "新topic" 2>&1)
S=$(state_get pingStreak); G=$(state_get gateStreak); LS=$(state_get lastShipAt); CT=$(state_get currentTopic)
check "Case10a note.py 輸出 NOTE_OK" "$(echo "$OUT" | grep -q NOTE_OK && echo 1 || echo 0)"
check "Case10b note.py 更新咗 currentTopic ($CT)" "$([ "$CT" = "新topic" ] && echo 1 || echo 0)"
check "Case10c note.py 冇掂 pingStreak ($S)/gateStreak ($G)/lastShipAt ($LS)" "$([ "$S" = "5" ] && [ "$G" = "5" ] && [ "$LS" = "12345" ] && echo 1 || echo 0)"

# ---------- Summary ----------
echo ""
echo "===== 結果：PASS=$PASS FAIL=$FAIL ====="
[ "$FAIL" = "0" ]
