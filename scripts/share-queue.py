#!/usr/bin/env python3
"""share-queue.py v2.1 — 隨時分享區（SQLite 版）。

v2.1（2026-08-31 竹取物語事件後大改）：
- 爬蟲式防重複三層，全部喺 add() 入倉時做（零 token，機械層）：
  Layer 1: content_hash 完全一樣（全歷史）→ skip
  Layer 2: canonical link seen-set（全歷史）→ 標 shared 留歷史
  Layer 3: SimHash near-dup（vs 最近 48h 已分享 + 所有 pending）→ 標 shared
- pick：出貨側機械揀貨（LLM 唔使再讀 memory / 對相似度）
- tidy：TTL 過期 + 重複 cluster 收埋（每週 cron 跑）

設計跟專業 DB 慣例 + 爬蟲界 dedup 慣例（Scrapy RFPDupeFilter / Google SimHash）：
- status 狀態機（pending → shared / discarded）
- 軟刪（status='shared' + shared_at），list 只撈 pending
- audit 欄：created_at / updated_at
- 去重：content_hash（md5）UNIQUE + canonical_link + simhash

 用法：
  python3 scripts/share-queue.py add --topic "..." --summary "..." [--link "url"] [--cat "海洋"] [--channel "news"] [--score 2] [--note "..."]
  python3 scripts/share-queue.py list [--channel X] [--topic X] [--guard N] [--all]
  python3 scripts/share-queue.py pick [--channel X] [--topic X] [--max-age 30] [--guard 48]   # 機械揀貨
  python3 scripts/share-queue.py scan [--fix] [--max-age 30] [--anomaly 20]                 # 健康檢查（dry-run；--fix 先清理）
  python3 scripts/share-queue.py recent [--hours 24]
  python3 scripts/share-queue.py remove --uuid <id>   # 軟刪（mark shared）
  python3 scripts/share-queue.py discard --uuid <id>  # 放棄（mark discarded）
  python3 scripts/share-queue.py tidy [--max-age 30]  # TTL + 重複 cluster 收埋
  python3 scripts/share-queue.py clear / stats
"""
import argparse
import datetime
import hashlib
import os
import re
import sqlite3
import uuid
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BASE = os.environ.get("IDLE_PING_WS") or os.path.expanduser("~/.openclaw/workspace")
DB_FILE = os.path.join(BASE, "curiosity", "share-queue.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS share_queue (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid            TEXT NOT NULL UNIQUE,
  topic           TEXT NOT NULL,
  summary         TEXT NOT NULL,
  link            TEXT,
  category        TEXT,
  interest_score  INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','shared','discarded')),
  channel         TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  shared_at       TEXT,
  note            TEXT,
  content_hash    TEXT NOT NULL UNIQUE,
  canonical_link  TEXT,
  simhash         INTEGER,
  title           TEXT,
  raw_hash        TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_queue_status_created
  ON share_queue (status, created_at);
"""

TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "campaign", "igshid", "mc_cid", "mc_eid",
}


# ────────────────────────── URL canonicalization ──────────────────────────

def canonicalize(url):
    """URL 標準化：剝 tracking 參數/fragment/default port，host 轉細楷。

    Scrapy 用 w3lib.url.canonicalize_url 做同一件事——我哋細規模版。
    """
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
        host = (p.hostname or "").lower()
        if not host:
            return url.strip()
        scheme = (p.scheme or "http").lower()
        port = p.port
        if port in (80, 443):
            port = None
        netloc = host + (f":{port}" if port else "")
        # 保留非 tracking query（唔郁 article id 呢類重要參數）
        q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING_KEYS]
        query = urlencode(q)
        path = p.path or ""
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return url.strip()


# ────────────────────────── SimHash（near-dup） ──────────────────────────

def _tokens(text):
    """CJK character bigram + 英文 word token（Google SimHash 做法嘅細規模版）。"""
    t = (text or "").lower()
    toks = []
    cjk = re.findall(r"[\u4e00-\u9fff]", t)
    toks += [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    toks += re.findall(r"[a-z0-9]+", t)
    return toks


def simhash(text):
    """64-bit SimHash fingerprint（TF 加權）。"""
    v = [0] * 64
    for tok, w in Counter(_tokens(text)).items():
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(64):
            if h & (1 << i):
                v[i] += w
            else:
                v[i] -= w
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= (1 << i)
    # SQLite INTEGER 係 signed 64-bit → 轉做 signed 先入到 DB
    if out >= (1 << 63):
        out -= (1 << 64)
    return out


def hamming(a, b):
    return bin(a ^ b).count("1")


def _similar(a, b):
    """兩段文字相似度（0-1），短文本 fallback 用。"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, (a or "").strip().lower(), (b or "").strip().lower()).ratio()


def normalize_title(title):
    """標題標準化（純 code，deterministic）：細楷 + 剝走所有非字母數字字符。"""
    if not title:
        return ""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (title or "").lower())


def raw_title_hash(title):
    """Raw source fingerprint：md5(normalize_title(title))。

    2026-08-31 用戶同意嘅原則：唔好用 AI output 餵 code 判斷——AI summary 係
    probabilistic（同一篇文章兩次歸納會出唔同 summary）→ 唔可以做 dedup 主材料；
    raw title 係 deterministic，同一個標題永遠出同一個 hash，先係可靠嘅 source 信號。
    太短（normalize 後 <10 字）唔用，避免「Breaking News」呢類通用標題誤撞。
    """
    t = normalize_title(title)
    if len(t) < 10:
        return None
    return hashlib.md5(t.encode("utf-8", "ignore")).hexdigest()


def content_hash(summary):
    return hashlib.md5(summary.strip().encode("utf-8", "ignore")).hexdigest()


# ────────────────────────── DB / migration ──────────────────────────

def get_conn():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # migration：舊 db 冇新 column → 加返 + backfill
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(share_queue)").fetchall()]
    if "channel" not in cols:
        conn.execute("ALTER TABLE share_queue ADD COLUMN channel TEXT")
    if "canonical_link" not in cols:
        conn.execute("ALTER TABLE share_queue ADD COLUMN canonical_link TEXT")
    if "simhash" not in cols:
        conn.execute("ALTER TABLE share_queue ADD COLUMN simhash INTEGER")
    if "title" not in cols:
        conn.execute("ALTER TABLE share_queue ADD COLUMN title TEXT")
    if "raw_hash" not in cols:
        conn.execute("ALTER TABLE share_queue ADD COLUMN raw_hash TEXT")
    conn.commit()
    # index 要喺 column 存在先建（舊 DB migration 次序問題）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_share_queue_canonical_link ON share_queue (canonical_link)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_share_queue_raw_hash ON share_queue (raw_hash)")
    conn.commit()
    # backfill canonical_link + simhash（只處理未填嘅，idempotent）
    rows = conn.execute(
        "SELECT id, link, summary FROM share_queue WHERE canonical_link IS NULL OR simhash IS NULL"
    ).fetchall()
    for r in rows:
        cl = canonicalize(r["link"]) if r["link"] else ""
        sh = simhash(r["summary"] or "")
        conn.execute(
            "UPDATE share_queue SET canonical_link=?, simhash=? WHERE id=?",
            (cl or None, sh, r["id"]),
        )
    conn.commit()
    # 現有重複 canonical_link 清掃：保留最早嗰條，其餘標 shared（唔會出貨）
    dupes = conn.execute(
        "SELECT canonical_link FROM share_queue "
        "WHERE canonical_link IS NOT NULL AND canonical_link != '' "
        "GROUP BY canonical_link HAVING COUNT(*) > 1"
    ).fetchall()
    for d in dupes:
        rows2 = conn.execute(
            "SELECT id FROM share_queue WHERE canonical_link=? ORDER BY id",
            (d["canonical_link"],),
        ).fetchall()
        for r in rows2[1:]:
            conn.execute(
                "UPDATE share_queue SET status='shared', "
                "shared_at=COALESCE(shared_at, datetime('now','localtime')), "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (r["id"],),
            )
    conn.commit()
    return conn


# ────────────────────────── add（唯一入口，三層防重複） ──────────────────────────

def _insert(conn, topic, summary, link, cat, score, note, channel, cl, sh, status="pending", title=""):
    conn.execute(
        "INSERT INTO share_queue (uuid, topic, summary, link, category, interest_score, note, content_hash, channel, canonical_link, simhash, title, raw_hash, status, shared_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, CASE WHEN ?='shared' THEN datetime('now','localtime') ELSE NULL END)",
        (
            uuid.uuid4().hex[:12], topic, summary, link or None, cat or None, score,
            note or None, content_hash(summary), channel or None, cl or None, sh,
            title or None, raw_title_hash(title), status, status,
        ),
    )
    conn.commit()
    return conn.execute("SELECT uuid FROM share_queue ORDER BY id DESC LIMIT 1").fetchone()["uuid"]


def add(topic, summary, link="", cat="", score=0, note="", channel="", title=""):
    conn = get_conn()
    cl = canonicalize(link)
    sh = simhash(summary)
    rh = raw_title_hash(title)
    cutoff48 = (datetime.datetime.now() - datetime.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    # Layer 1：content_hash 完全一樣（全歷史）→ skip
    dup = conn.execute(
        "SELECT uuid, topic, status FROM share_queue WHERE content_hash=?",
        (content_hash(summary),),
    ).fetchone()
    if dup:
        print(f"⚠️ 重複內容（content_hash 一樣，同 [{dup['uuid']}] {dup['status']}），跳過——同一條嘢唔會入兩次")
        conn.close()
        return

    # Layer 2：canonical link seen-set（全歷史）→ 標 shared 留歷史
    if cl:
        dup = conn.execute(
            "SELECT uuid, topic, status FROM share_queue WHERE canonical_link=?",
            (cl,),
        ).fetchone()
        if dup:
            _insert(conn, topic, summary, link, cat, score, note, channel, cl, sh, status="shared", title=title)
            print(f"🔁 偵測到重複（link 一樣，同 [{dup['uuid']}] {dup['status']}）→ 自動標記為 shared，唔會出貨")
            conn.close()
            return

    # Layer 2.5：raw title hash（deterministic source 信號）→ 標 shared
    #    （AI summary 係 probabilistic 唔可以做 dedup 主材料；raw title 先可靠）
    if rh:
        dup = conn.execute(
            "SELECT uuid, topic, status FROM share_queue WHERE raw_hash=?",
            (rh,),
        ).fetchone()
        if dup:
            _insert(conn, topic, summary, link, cat, score, note, channel, cl, sh, status="shared", title=title)
            print(f"🔁 偵測到重複（raw title hash 一樣，同 [{dup['uuid']}] {dup['status']}）→ 自動標記為 shared，唔會出貨")
            conn.close()
            return

    # Layer 3：near-dup（vs 最近 48h 已分享 + 所有 pending）
    # 2026-08-31 測試後修正：SimHash ≤3 對 CJK 短文本唔可靠（真 paraphrase 都 24-bit distance）
    # → 主偵測用 SequenceMatcher ≥ 0.55（paraphrase 0.64 vs 無關 0.09，分得好清）
    #   SimHash 留做 secondary signal（長文檔/未來擴容用），存喺 DB 唔晒
    cands = conn.execute(
        "SELECT uuid, topic, summary, simhash FROM share_queue "
        "WHERE (status='shared' AND shared_at >= ?) OR status='pending'",
        (cutoff48,),
    ).fetchall()
    for r in cands:
        if _similar(summary, r["summary"]) >= 0.55:
            _insert(conn, topic, summary, link, cat, score, note, channel, cl, sh, status="shared", title=title)
            print(f"🔁 偵測到重複（summary 相似度，同 [{r['uuid']}]）→ 自動標記為 shared，唔會出貨")
            conn.close()
            return
        if (r["simhash"] is not None) and hamming(sh, r["simhash"]) <= 3:
            _insert(conn, topic, summary, link, cat, score, note, channel, cl, sh, status="shared", title=title)
            print(f"🔁 偵測到重複（SimHash near-dup，同 [{r['uuid']}]）→ 自動標記為 shared，唔會出貨")
            conn.close()
            return

    # 全新 → pending
    try:
        uid = _insert(conn, topic, summary, link, cat, score, note, channel, cl, sh, status="pending", title=title)
        print(f"✅ 已加入分享區 [{uid}] {topic}")
    except sqlite3.IntegrityError:
        print("⚠️ 重複內容（content_hash 一樣），跳過——同一條嘢唔會入兩次")
    finally:
        conn.close()


# ────────────────────────── list / recent ──────────────────────────

def list_items(show_all=False, channel="", topic="", guard_hours=0):
    conn = get_conn()
    sql = "SELECT * FROM share_queue"
    conds, params = [], []
    if not show_all:
        conds.append("status='pending'")
    if channel:
        conds.append("channel=?")
        params.append(channel)
    if topic:
        conds.append("(topic LIKE ? OR summary LIKE ?)")
        params += [f"%{topic}%", f"%{topic}%"]
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    # --guard N：濾走同「最近 N 小時內已分享」相似嘅貨（防重複出貨）
    if guard_hours > 0:
        cutoff = (datetime.datetime.now() - datetime.timedelta(hours=guard_hours)).strftime("%Y-%m-%d %H:%M:%S")
        recent = conn.execute(
            "SELECT topic, summary FROM share_queue WHERE status='shared' AND shared_at >= ?",
            (cutoff,),
        ).fetchall()
        recent_texts = [(r["topic"] or "", r["summary"] or "") for r in recent]
        kept = []
        for r in rows:
            cand = (r["topic"] or "") + " " + (r["summary"] or "")
            if any(_similar(cand, rt + " " + rs) >= 0.55 for rt, rs in recent_texts):
                continue  # 同最近出過嘅貨太似 → 唔顯示
            kept.append(r)
        rows = kept
    conn.close()
    if not rows:
        print("（分享區係空嘅）" if not show_all else "（歷史都係空嘅）")
        return
    print(f"📥 分享區有 {len(rows)} 條：")
    for i, r in enumerate(rows, 1):
        tag = {"pending": "⏳", "shared": "✅", "discarded": "🗑️"}.get(r["status"], "•")
        print(f"{i}. {tag} [{r['uuid']}] {r['topic']}｜{r['summary'][:55]}")
        if r["link"]:
            print(f"   來源: {r['link']}")
        if r["category"] or r["interest_score"]:
            print(f"   類別: {r['category'] or '—'} · 管道: {r['channel'] or '—'} · 有趣度: {r['interest_score']} · {r['status']} @ {r['created_at']}")
        if r["note"]:
            print(f"   諗法: {r['note']}")


def recent(hours=24):
    """最近 N 小時內已分享嘅貨（防重複出貨對照用）。"""
    conn = get_conn()
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT topic, summary, shared_at, channel FROM share_queue "
        "WHERE status='shared' AND shared_at >= ? ORDER BY shared_at DESC",
        (cutoff,),
    ).fetchall()
    conn.close()
    if not rows:
        print(f"（最近 {hours} 小時冇出過貨）")
        return
    print(f"📤 最近 {hours} 小時內已分享 {len(rows)} 條：")
    for r in rows:
        print(f"- [{r['shared_at'][11:16]}] {r['topic']}｜{r['summary'][:50]}")


# ────────────────────────── pick（出貨側機械揀貨） ──────────────────────────

def pick(channel="", topic="", max_age_days=30, guard_hours=48):
    """機械揀一條貨：唔會揀今日已出過 / 同最近 guard_hours 相似 / 太舊嘅貨。

    輸出格式（idle-ping-send 讀呢個）：
      PICK <uuid>
      TOPIC <topic>
      SUMMARY <summary>
      LINK <link>      （optional）
      CHANNEL <channel>
    冇貨 → 輸出 NONE
    """
    conn = get_conn()
    cutoff_age = (datetime.datetime.now() - datetime.timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M:%S")
    conds = ["status='pending'", "created_at >= ?"]
    params = [cutoff_age]
    if channel:
        conds.append("channel=?")
        params.append(channel)
    if topic:
        conds.append("(topic LIKE ? OR summary LIKE ?)")
        params += [f"%{topic}%", f"%{topic}%"]
    rows = conn.execute(
        "SELECT * FROM share_queue WHERE " + " AND ".join(conds) + " ORDER BY created_at DESC",
        params,
    ).fetchall()
    if not rows:
        print("NONE")
        conn.close()
        return

    # 攞最近 shared 做對照（guard_hours）+ 今日已出 topic
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=guard_hours)).strftime("%Y-%m-%d %H:%M:%S")
    recent = conn.execute(
        "SELECT topic, summary, canonical_link, shared_at FROM share_queue "
        "WHERE status='shared' AND shared_at >= ?",
        (cutoff,),
    ).fetchall()
    recent_texts = [(r["topic"] or "", r["summary"] or "") for r in recent]
    recent_links = {r["canonical_link"] for r in recent if r["canonical_link"]}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    today_topics = {r["topic"] for r in recent if (r["shared_at"] or "").startswith(today)}

    kept = []
    for r in rows:
        cand = (r["topic"] or "") + " " + (r["summary"] or "")
        if any(_similar(cand, rt + " " + rs) >= 0.55 for rt, rs in recent_texts):
            continue  # 同最近出過嘅貨太似
        if r["canonical_link"] and r["canonical_link"] in recent_links:
            continue  # link 最近出過
        if r["topic"] and r["topic"] in today_topics:
            continue  # 今日已出過同 topic
        kept.append(r)
    conn.close()
    if not kept:
        print("NONE")
        return

    # 排序：interest_score 高優先，其次新入倉
    kept.sort(key=lambda r: (r["interest_score"] or 0, r["created_at"] or ""), reverse=True)
    best = kept[0]
    print(f"PICK {best['uuid']}")
    print(f"TOPIC {best['topic']}")
    print(f"SUMMARY {best['summary']}")
    if best["link"]:
        print(f"LINK {best['link']}")
    print(f"CHANNEL {best['channel'] or ''}")


# ────────────────────────── tidy（TTL + 重複 cluster） ──────────────────────────

def tidy(max_age_days=30):
    """倉維護：TTL + 遺留重複收埋。

    1) pending 太舊 → discarded
    2) pending 同已 shared 貨相似（summary ≥0.55）→ 標 shared（唔會再出貨）
    3) pending-pending 互相相似 → 保留最好嗰條（interest_score 高優先，其次新），其餘 discarded

    每週 cron 跑一次（delivery none），令倉保持細同乾淨。
    """
    conn = get_conn()
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M:%S")
    # 1) 過期 pending
    n1 = conn.execute(
        "UPDATE share_queue SET status='discarded', updated_at=datetime('now','localtime') "
        "WHERE status='pending' AND created_at < ?",
        (cutoff,),
    ).rowcount

    # 2) pending 撞已 shared（summary 相似度 ≥0.55）→ 標 shared
    #    ⚠️ 唔用 link 做主信號：遺留數據入面 Google News RSS link 有 artifact（唔同文章共用同一條 link，
    #    例如 id=57 日本MMX vs id=58 香港街名），link-only match 會誤殺；summary 相似度先係可靠信號
    #    2026-08-31 補：loop 到 fixpoint——新標 shared 嘅貨可能令其他 pending 撞到（transitive chain），
    #    淨係跑一次會漏（實例：id=191/462/644 嘅孖生兄弟喺同一輪被標 shared，佢哋自己對舊 shared 集合 <0.55 逃過）
    n2 = 0
    while True:
        pend_rows = conn.execute(
            "SELECT id, topic, summary FROM share_queue WHERE status='pending'"
        ).fetchall()
        shared_texts = [(r["topic"] or "", r["summary"] or "") for r in conn.execute(
            "SELECT topic, summary FROM share_queue WHERE status='shared'"
        ).fetchall()]
        marked = 0
        for p in pend_rows:
            cand = (p["topic"] or "") + " " + (p["summary"] or "")
            if any(_similar(cand, st + " " + ss) >= 0.55 for st, ss in shared_texts):
                conn.execute(
                    "UPDATE share_queue SET status='shared', shared_at=datetime('now','localtime'), "
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    (p["id"],),
                )
                marked += 1
        conn.commit()
        n2 += marked
        if marked == 0:
            break

    # 3) pending-pending 互相相似 → 保留最好，其餘 discarded
    n3 = 0
    pend2 = [dict(r) for r in conn.execute(
        "SELECT id, uuid, topic, summary, interest_score, created_at, simhash "
        "FROM share_queue WHERE status='pending'"
    ).fetchall()]
    # 由高分/最新排起，保留前面，剷走後面相似嗰啲
    pend2.sort(key=lambda r: (r["interest_score"] or 0, r["created_at"] or ""), reverse=True)
    kept_ids = set()
    for i in range(len(pend2)):
        if pend2[i]["id"] in kept_ids:
            continue
        for j in range(i + 1, len(pend2)):
            if pend2[j]["id"] in kept_ids:
                continue
            a = (pend2[i]["topic"] or "") + " " + (pend2[i]["summary"] or "")
            b = (pend2[j]["topic"] or "") + " " + (pend2[j]["summary"] or "")
            sim = _similar(a, b)
            sh_ok = (pend2[i]["simhash"] is not None and pend2[j]["simhash"] is not None
                     and hamming(pend2[i]["simhash"], pend2[j]["simhash"]) <= 3)
            if sim >= 0.55 or sh_ok:
                conn.execute(
                    "UPDATE share_queue SET status='discarded', updated_at=datetime('now','localtime') WHERE id=?",
                    (pend2[j]["id"],),
                )
                kept_ids.add(pend2[j]["id"])
                n3 += 1
    conn.commit()
    conn.close()
    print(f"🧹 tidy 完成：過期剷咗 {n1} 條｜pending-撞-shared 標 shared {n2} 條｜pending-pending 收埋 {n3} 條")


# ────────────────────────── scan（每日健康檢查） ──────────────────────────

def scan(fix=False, max_age_days=30, threshold=0.55, anomaly=20):
    """倉庫健康檢查（dry-run 報告；--fix 先會實際清理）。

    2026-08-31 用戶要求：每日自動 scan 一次 DB 睇有冇重複。
    純 Python 零 token，crontab 04:30 跑；異常（撞 shared + 近重 > anomaly）
    會自動寫 system-activity.log，heartbeat 見到會主動同用戶講。
    """
    conn = get_conn()
    pend = conn.execute(
        "SELECT id, uuid, topic, summary, interest_score, created_at, simhash "
        "FROM share_queue WHERE status='pending'"
    ).fetchall()
    shared = conn.execute("SELECT topic, summary FROM share_queue WHERE status='shared'").fetchall()
    shared_texts = [(r["topic"] or "", r["summary"] or "") for r in shared]
    total = conn.execute("SELECT COUNT(*) c FROM share_queue").fetchone()["c"]
    n_disc = conn.execute("SELECT COUNT(*) c FROM share_queue WHERE status='discarded'").fetchone()["c"]

    # 1) pending 撞已 shared（summary 相似度 ≥ threshold）
    dup_shared = []
    for p in pend:
        cand = (p["topic"] or "") + " " + (p["summary"] or "")
        if any(_similar(cand, st + " " + ss) >= threshold for st, ss in shared_texts):
            dup_shared.append(p)

    # 2) pending-pending 近重
    pend_list = [dict(r) for r in pend]
    dup_pairs = []
    for i in range(len(pend_list)):
        for j in range(i + 1, len(pend_list)):
            a = (pend_list[i]["topic"] or "") + " " + (pend_list[i]["summary"] or "")
            b = (pend_list[j]["topic"] or "") + " " + (pend_list[j]["summary"] or "")
            if _similar(a, b) >= threshold:
                dup_pairs.append((pend_list[i]["id"], pend_list[j]["id"]))

    # 3) pending link 重複組
    link_dups = conn.execute(
        "SELECT canonical_link, COUNT(*) c FROM share_queue "
        "WHERE status='pending' AND canonical_link IS NOT NULL AND canonical_link != '' "
        "GROUP BY canonical_link HAVING c > 1"
    ).fetchall()
    conn.close()

    print(f"🔍 scan：總共 {total}｜pending {len(pend)}｜shared {len(shared)}｜discarded {n_disc}")
    print(f"   pending-撞-shared：{len(dup_shared)} 條｜pending-pending 近重：{len(dup_pairs)} 對｜pending link 重複組：{len(link_dups)} 組")
    for p in dup_shared[:10]:
        print(f"   ⚠️ id={p['id']} {p['topic'][:30]}｜{p['summary'][:40]}")
    for a, b in dup_pairs[:10]:
        print(f"   🧩 id={a} ↔ id={b}")

    issues = len(dup_shared) + len(dup_pairs)
    if issues > anomaly:
        import subprocess
        subprocess.run(
            ["bash", os.path.join(BASE, "scripts", "system-log.sh"),
             f"⚠️ 倉庫異常：scan 發現 {len(dup_shared)} 條撞 shared + {len(dup_pairs)} 對近重（>{anomaly}）"],
            capture_output=True,
        )
        print(f"   ⚠️ ANOMALY：{issues} 個問題 > {anomaly}，已寫 system-activity.log")

    if fix and issues > 0:
        print("\n→ 執行清理（tidy）...")
        tidy(max_age_days)


# ────────────────────────── 其他 ──────────────────────────

def soft_delete(conn, uuid_val, status, field):
    cur = conn.execute(
        f"UPDATE share_queue SET status=?, {field}=datetime('now','localtime'), "
        f"updated_at=datetime('now','localtime') WHERE uuid=? AND status='pending'",
        (status, uuid_val),
    )
    conn.commit()
    return cur.rowcount


def remove(uuid_val):
    conn = get_conn()
    n = soft_delete(conn, uuid_val, "shared", "shared_at")
    conn.close()
    if n:
        print(f"🗑️ 已分享 [{uuid_val}]（軟刪，歷史保留）")
    else:
        print(f"❌ 搵唔到 pending 記錄 {uuid_val}")


def discard(uuid_val):
    conn = get_conn()
    n = soft_delete(conn, uuid_val, "discarded", "shared_at")
    conn.close()
    if n:
        print(f"🗑️ 已放棄 [{uuid_val}]")
    else:
        print(f"❌ 搵唔到 pending 記錄 {uuid_val}")


def clear():
    conn = get_conn()
    n = conn.execute("DELETE FROM share_queue WHERE status='pending'").rowcount
    conn.commit()
    conn.close()
    print(f"🧹 已清空 {n} 條 pending")


def stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM share_queue").fetchone()["c"]
    pending = conn.execute("SELECT COUNT(*) c FROM share_queue WHERE status='pending'").fetchone()["c"]
    shared = conn.execute("SELECT COUNT(*) c FROM share_queue WHERE status='shared'").fetchone()["c"]
    discarded = conn.execute("SELECT COUNT(*) c FROM share_queue WHERE status='discarded'").fetchone()["c"]
    avg = conn.execute("SELECT AVG(interest_score) a FROM share_queue WHERE status='shared'").fetchone()["a"]
    by_cat = conn.execute(
        "SELECT category, COUNT(*) c FROM share_queue WHERE status='shared' GROUP BY category ORDER BY c DESC LIMIT 5"
    ).fetchall()
    conn.close()
    print(f"📊 分享區統計：總共 {total}｜pending {pending}｜已分享 {shared}｜放棄 {discarded}")
    if avg:
        print(f"   已分享平均有趣度: {avg:.1f}")
    if by_cat:
        print("   最常分享類別:", ", ".join(f"{r['category'] or '—'}×{r['c']}" for r in by_cat))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--topic", required=True)
    p_add.add_argument("--summary", required=True)
    p_add.add_argument("--link", default="")
    p_add.add_argument("--cat", default="")
    p_add.add_argument("--channel", default="", help="來源管道（news/onthisday/arxiv/reddit/randomwiki/misconceptions/xsearch）")
    p_add.add_argument("--score", type=int, default=0)
    p_add.add_argument("--note", default="")
    p_add.add_argument("--title", default="", help="raw 標題（crawler 傳，用嚟做 deterministic raw hash dedup）")

    p_list = sub.add_parser("list")
    p_list.add_argument("--all", action="store_true", help="連歷史一齊睇")
    p_list.add_argument("--channel", default="", help="只顯示指定管道（news/onthisday/arxiv/reddit 等）")
    p_list.add_argument("--topic", default="", help="只顯示 topic/summary 含關鍵字嘅貨")
    p_list.add_argument("--guard", type=int, default=0, help="濾走同最近 N 小時內已分享相似嘅貨（防重複出貨）")

    p_pick = sub.add_parser("pick")
    p_pick.add_argument("--channel", default="", help="指定管道揀貨")
    p_pick.add_argument("--topic", default="", help="指定 topic 關鍵字揀貨")
    p_pick.add_argument("--max-age", type=int, default=30, help="唔揀超過 N 日嘅貨（TTL，預設 30）")
    p_pick.add_argument("--guard", type=int, default=48, help="同最近 N 小時已分享相似嘅貨唔揀（預設 48）")

    p_recent = sub.add_parser("recent")
    p_recent.add_argument("--hours", type=int, default=24, help="顯示最近 N 小時內已分享嘅貨")

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--fix", action="store_true", help="發現重複就實際清理（tidy）")
    p_scan.add_argument("--max-age", type=int, default=30, help="tidy TTL（預設 30 日）")
    p_scan.add_argument("--anomaly", type=int, default=20, help="超過 N 個問題就寫 system log（預設 20）")

    p_tidy = sub.add_parser("tidy")
    p_tidy.add_argument("--max-age", type=int, default=30, help="pending 超過 N 日自動過期（預設 30）")

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--uuid", required=True)

    p_discard = sub.add_parser("discard")
    p_discard.add_argument("--uuid", required=True)

    sub.add_parser("clear")
    sub.add_parser("stats")

    args = parser.parse_args()
    if args.cmd == "add":
        add(args.topic, args.summary, args.link, args.cat, args.score, args.note, args.channel, args.title)
    elif args.cmd == "list":
        list_items(args.all, args.channel, args.topic, args.guard)
    elif args.cmd == "pick":
        pick(args.channel, args.topic, args.max_age, args.guard)
    elif args.cmd == "recent":
        recent(args.hours)
    elif args.cmd == "scan":
        scan(args.fix, args.max_age, anomaly=args.anomaly)
    elif args.cmd == "tidy":
        tidy(args.max_age)
    elif args.cmd == "remove":
        remove(args.uuid)
    elif args.cmd == "discard":
        discard(args.uuid)
    elif args.cmd == "clear":
        clear()
    elif args.cmd == "stats":
        stats()


if __name__ == "__main__":
    main()
