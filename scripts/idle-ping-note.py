#!/usr/bin/env python3
"""idle-ping-note.py — 出貨簿記（機械層，2026-09-01 建立）

背景：2026-09-01 事件——idle-ping-send（LLM agent）喺步驟 7 直接讀寫 state.json，
用舊快照覆蓋咗 gate 寫入嘅 pingStreak（2 → 1），搞到「出咗 4 次貨先暫停」。

規則：LLM 唔准直接讀寫 state.json 嘅 gate 地盤 key
（pingStreak / lastShipAt / lastPingTriggerAt / lastPauseNoticeAt / pauseNoticeSent / dailyCount / dailyDate）。
呢個 script 係唯一允許嘅寫入途徑，而且只更新 lastTopics / currentTopic。

用法：
    python3 idle-ping-note.py --topic "<今次探索 topic>"

輸出：NOTE_OK（成功）／ERROR <原因>（失敗，exit code 1）
"""
import argparse
import json
import os
import sys

# 2026-09-01 加：支援環境變數 override（sandbox 測試用，同 gate script 風格一致）；
# 預設用 ~ 展開（唔 hardcode 用戶名，公開 repo 安全）
STATE = os.environ.get(
    "IDLE_PING_STATE_JSON",
    os.path.expanduser("~/.openclaw/workspace/curiosity/state.json"),
)
MAX_TOPICS = 20


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True, help="今次探索嘅 topic")
    args = ap.parse_args()
    topic = args.topic.strip()
    if not topic:
        print("ERROR empty topic")
        return 1

    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception as e:  # noqa: BLE001 - 簿記唔應該令出貨流程死
        print(f"ERROR read state: {e}")
        return 1

    # 只准改呢兩個 key——其餘（pingStreak/lastShipAt/lastPingTriggerAt/...）一律唔掂
    topics = [t for t in s.get("lastTopics", []) if t != topic]
    topics.insert(0, topic)
    s["currentTopic"] = topic
    s["lastTopics"] = topics[:MAX_TOPICS]

    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR write state: {e}")
        return 1

    print("NOTE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
