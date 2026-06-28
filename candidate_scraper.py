#!/usr/bin/env python3
"""
候選人動態爬蟲 v3 - imaid.com.tw
頁面為 JavaScript 動態渲染，使用 Playwright 執行真實瀏覽器
不需要帳號密碼，頁面為公開可存取
"""
import re, json, datetime, urllib.request, urllib.error, os, sys

FIREBASE_PROJECT = "adamcv-79d47"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyAB-b45xKSpUg5I43NUxZtzjytj9p5Fu_A")
FIRESTORE_BASE   = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"
SCRAPE_URL       = "https://www.imaid.com.tw/biox/index.php?fun=resume&sales=adam"

COUNTRY_MAP = {
    "SJ":"印尼","SN":"印尼","IN":"印尼",
    "MKB":"菲律賓","KB":"菲律賓","CL":"菲律賓","PH":"菲律賓",
    "TB":"泰國","TH":"泰國",
    "VN":"越南","VT":"越南",
    "MM":"緬甸",
}

# ── Firestore REST ────────────────────────────────────────────────────────────

def _fs(method, path, body=None):
    url  = f"{FIRESTORE_BASE}/{path}?key={FIREBASE_API_KEY}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404: return None
        raise

def fs_get(col, id):     return _fs("GET",   f"{col}/{id}")
def fs_set(col, id, d):  return _fs("PATCH", f"{col}/{id}", {"fields": _fields(d)})
def fs_add(col, d):      return _fs("POST",   col,          {"fields": _fields(d)})

def _fields(d):
    out = {}
    for k, v in d.items():
        if isinstance(v, list):
            out[k] = {"arrayValue": {"values": [{"stringValue": str(i)} for i in v]}}
        elif isinstance(v, int):
            out[k] = {"integerValue": str(v)}
        else:
            out[k] = {"stringValue": str(v)}
    return out

def field(doc, key):
    if not doc or "fields" not in doc: return None
    f = doc["fields"].get(key, {})
    if "stringValue"  in f: return f["stringValue"]
    if "integerValue" in f: return int(f["integerValue"])
    if "arrayValue"   in f:
        return [v.get("stringValue","") for v in f["arrayValue"].get("values",[])]
    return None

# ── Playwright scraper (no login needed) ─────────────────────────────────────

def scrape():
    from playwright.sync_api import sync_playwright
    print(f"  Opening {SCRAPE_URL} ...")
    candidates = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SCRAPE_URL, wait_until="networkidle", timeout=30000)
        # Wait for JS to render candidate list
        page.wait_for_selector(".lists", timeout=15000)
        items = page.query_selector_all(".lists")
        print(f"  Found {len(items)} items")
        seen = set()
        id_re = re.compile(r'\b([A-Z]{1,3}[A-Z]\.\d+)\b')
        for item in items:
            text = re.sub(r'\s+', ' ', item.inner_text()).strip()
            m = id_re.search(text)
            if not m or m.group(1) in seen: continue
            cid = m.group(1); seen.add(cid)
            prefix = re.match(r'^([A-Z]+)\.', cid)
            p_str = prefix.group(1) if prefix else ''
            candidates.append({
                "candidateId": cid,
                "age":     re.search(r'[♂♀](\d+)', text).group(1) if re.search(r'[♂♀](\d+)', text) else '',
                "marital": next((x for x in ['已婚','未婚'] if x in text), ''),
                "height":  re.search(r'(\d{3})\s*cm', text).group(1) if re.search(r'(\d{3})\s*cm', text) else '',
                "country": COUNTRY_MAP.get(p_str, p_str),
            })
        browser.close()
    return candidates

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().isoformat()
    print(f"\n=== 候選人動態爬蟲 {today} ===")

    current = scrape()
    current_ids = set(c["candidateId"] for c in current)
    current_map = {c["candidateId"]: c for c in current}
    print(f"  ➡️  Current: {len(current_ids)} candidates")

    if len(current_ids) == 0:
        print("  ❌ Got 0 candidates — check if page structure changed")
        sys.exit(1)

    snap = fs_get("candidate_snapshots", "latest")
    prev_ids = set(field(snap, "ids") or []) if snap else set()
    print(f"  ➡️  Previous snapshot: {len(prev_ids)}")

    added   = current_ids - prev_ids
    removed = prev_ids    - current_ids
    print(f"  ➕ Added: {len(added)}, ➖ Removed: {len(removed)}")

    written = 0
    if snap:
        for cid in sorted(added):
            info = current_map.get(cid, {})
            fs_add("candidate_changes", {
                "date": today, "type": "added", "candidateId": cid,
                "age": info.get("age",""), "marital": info.get("marital",""),
                "height": info.get("height",""), "country": info.get("country",""),
            })
            print(f"    ✅ Added {cid}")
            written += 1
        for cid in sorted(removed):
            fs_add("candidate_changes", {
                "date": today, "type": "removed", "candidateId": cid,
                "age":"","marital":"","height":"","country":"",
            })
            print(f"    ❌ Removed {cid}")
            written += 1

    fs_set("candidate_snapshots", "latest", {
        "ids": sorted(current_ids), "updatedAt": today, "count": len(current_ids),
    })
    print(f"\n✅ Done. Snapshot updated ({len(current_ids)} candidates), {written} changes written.")

if __name__ == "__main__":
    main()
