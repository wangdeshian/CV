#!/usr/bin/env python3
"""
候選人動態爬蟲 - imaid.com.tw
每日執行：抓取候選人清單，與 Firestore 快照比較，異動寫入 candidate_changes
"""
import re
import json
import datetime
import urllib.request
import urllib.error
import os

FIREBASE_PROJECT = "adamcv-79d47"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyAB-b45xKSpUg5I43NUxZtzjytj9p5Fu_A")
FIRESTORE_BASE   = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"
SCRAPE_URL       = "https://www.imaid.com.tw/biox/index.php?fun=resume&sales=adam"
HEADERS          = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.imaid.com.tw/",
}

# ── Firestore REST helpers ──────────────────────────────────────────────────

def _fs_request(method, path, body=None):
    url = f"{FIRESTORE_BASE}/{path}?key={FIREBASE_API_KEY}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def fs_get(col, doc_id):
    return _fs_request("GET", f"{col}/{doc_id}")

def fs_set(col, doc_id, data):
    fields = _to_fields(data)
    return _fs_request("PATCH", f"{col}/{doc_id}", {"fields": fields})

def fs_add(col, data):
    fields = _to_fields(data)
    return _fs_request("POST", col, {"fields": fields})

def _to_fields(data):
    fields = {}
    for k, v in data.items():
        if isinstance(v, list):
            fields[k] = {"arrayValue": {"values": [{"stringValue": str(i)} for i in v]}}
        elif isinstance(v, bool):
            fields[k] = {"booleanValue": v}
        elif isinstance(v, int):
            fields[k] = {"integerValue": str(v)}
        else:
            fields[k] = {"stringValue": str(v)}
    return fields

def fs_field(doc, key):
    if not doc or "fields" not in doc:
        return None
    f = doc["fields"].get(key, {})
    if "stringValue" in f:   return f["stringValue"]
    if "integerValue" in f:  return int(f["integerValue"])
    if "booleanValue" in f:  return f["booleanValue"]
    if "arrayValue" in f:
        return [v.get("stringValue", "") for v in f["arrayValue"].get("values", [])]
    return None

# ── Scraper ─────────────────────────────────────────────────────────────────

COUNTRY_MAP = {
    "SJ": "印尼", "SN": "印尼", "IN": "印尼",
    "MKB": "菲律賓", "KB": "菲律賓", "CL": "菲律賓", "PH": "菲律賓",
    "TB": "泰國", "TH": "泰國",
    "VN": "越南", "VT": "越南",
    "MM": "緬甸",
}

def parse_candidates(html):
    candidates = []
    seen = set()
    id_re = re.compile(r'\b([A-Z]{1,3}[A-Z]\.\d+)\b')
    blocks = re.findall(r'class="lists"[^>]*>(.*?)</div>', html, re.DOTALL)
    for block in blocks:
        clean = re.sub(r'<[^>]+>', ' ', block)
        clean = re.sub(r'\s+', ' ', clean).strip()
        m = id_re.search(clean)
        if not m or m.group(1) in seen:
            continue
        cid = m.group(1)
        seen.add(cid)
        prefix = re.match(r'^([A-Z]+)\.', cid)
        p = prefix.group(1) if prefix else ''
        candidates.append({
            "candidateId": cid,
            "age":    (re.search(r'[♂♀](\d+)', clean) or type('', (), {'group': lambda *a: ''})()).group(1) if re.search(r'[♂♀](\d+)', clean) else '',
            "marital": next((x for x in ['已婚','未婚'] if x in clean), ''),
            "height": (re.search(r'(\d{3})\s*cm', clean) or type('', (), {'group': lambda *a: ''})()).group(1) if re.search(r'(\d{3})\s*cm', clean) else '',
            "country": COUNTRY_MAP.get(p, p),
        })
    return candidates

def scrape():
    req = urllib.request.Request(SCRAPE_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode('utf-8', errors='replace')
    return parse_candidates(html)

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().isoformat()
    print(f"\n=== 候選人動態爬蟲 {today} ===")

    print("Scraping imaid.com.tw ...")
    current = scrape()
    current_ids = set(c["candidateId"] for c in current)
    current_map = {c["candidateId"]: c for c in current}
    print(f"  ➡️  Current candidates: {len(current_ids)}")

    snap = fs_get("candidate_snapshots", "latest")
    prev_ids = set(fs_field(snap, "ids") or []) if snap else set()
    print(f"  ➡️  Previous snapshot:  {len(prev_ids)} {'(first run)' if not snap else ''}")

    added   = current_ids - prev_ids
    removed = prev_ids    - current_ids
    print(f"  ➕ Added:   {len(added)}")
    print(f"  ➖ Removed: {len(removed)}")

    written = 0
    if snap:
        for cid in sorted(added):
            info = current_map.get(cid, {})
            fs_add("candidate_changes", {
                "date": today, "type": "added",
                "candidateId": cid,
                "age": info.get("age",""), "marital": info.get("marital",""),
                "height": info.get("height",""), "country": info.get("country",""),
            })
            print(f"    ✅ Added {cid}")
            written += 1
        for cid in sorted(removed):
            fs_add("candidate_changes", {
                "date": today, "type": "removed",
                "candidateId": cid,
                "age": "", "marital": "", "height": "", "country": "",
            })
            print(f"    ❌ Removed {cid}")
            written += 1

    fs_set("candidate_snapshots", "latest", {
        "ids": sorted(current_ids),
        "updatedAt": today,
        "count": len(current_ids),
    })

    print(f"\n✅ Done. Snapshot updated ({len(current_ids)} candidates), {written} changes written.")
    if not snap:
        print("ℹ️  First run: baseline saved. Changes will appear from tomorrow.")

if __name__ == "__main__":
    main()
