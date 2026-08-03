#!/usr/bin/env python3
"""
更新候選人列表與詳細履歷。

GitHub Actions 會先執行本腳本，產生：
- work/imaid/candidates-list.json
- work/imaid/detail-<candidateId>.html

後續配對腳本會直接使用這些最新資料。
"""

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "work" / "imaid"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BASE = "https://www.imaid.com.tw/biox/index.php"
LIST_URL = BASE + "?fun=resume&sales=adam&page={page}&st=DESC&od=L&search=&stype="
FIREBASE_PROJECT = "adamcv-79d47"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyAB-b45xKSpUg5I43NUxZtzjytj9p5Fu_A")
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"
MIN_REASONABLE_CANDIDATES = 20
MAX_DROP_RATIO = 0.45


def fetch_text(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CV-auto-match/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def _fs(method, path, body=None):
    sep = "&" if "?" in path else "?"
    url = f"{FIRESTORE_BASE}/{path}{sep}key={FIREBASE_API_KEY}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _fields(data):
    out = {}
    for key, value in data.items():
        if isinstance(value, list):
            out[key] = {"arrayValue": {"values": [{"stringValue": str(item)} for item in value]}}
        elif isinstance(value, int):
            out[key] = {"integerValue": str(value)}
        else:
            out[key] = {"stringValue": str(value)}
    return out


def field(doc, key):
    if not doc or "fields" not in doc:
        return None
    item = doc["fields"].get(key, {})
    if "stringValue" in item:
        return item["stringValue"]
    if "integerValue" in item:
        return int(item["integerValue"])
    if "arrayValue" in item:
        return [v.get("stringValue", "") for v in item["arrayValue"].get("values", [])]
    return None


def change_doc_id(date, change_type, candidate_id):
    safe_candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", candidate_id).strip("_")
    return f"{date}_{change_type}_{safe_candidate}"


def write_candidate_change(date, change_type, candidate_id):
    doc_id = change_doc_id(date, change_type, candidate_id)
    _fs("PATCH", f"candidate_changes/{doc_id}", {"fields": _fields({
        "date": date,
        "type": change_type,
        "candidateId": candidate_id,
    })})


def clean_text(value):
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_total_pages(text):
    m = re.search(r'id\s*=\s*["\']tpg["\']\s+value\s*=\s*["\'](\d+)["\']', text)
    return int(m.group(1)) if m else 1


def parse_list_page(text, page_no):
    items = []
    blocks = re.split(r'<div class\s*=\s*["\']lists["\']', text)[1:]
    for block in blocks:
        code_m = re.search(r'id=["\']shownom?ber2["\']\s*>\s*([^<\s]+)', block)
        id_m = re.search(r'id=([a-f0-9]{24,40})', block)
        if not code_m or not id_m:
            continue

        code = html.unescape(code_m.group(1)).strip()
        resume_id = id_m.group(1)
        block_text = clean_text(block)

        age_m = re.search(r'[♀♂]\s*(\d{1,2})', block_text)
        hw_m = re.search(r'(\d{3})\s*cm\s*/\s*(\d{2,3})\s*kg', block_text, re.I)
        flags = re.findall(r'alt\s*=\s*["\']([^"\']+\d{4}[-~]\d{4})["\']', block)
        name_m = re.search(r'listname=([^&"\']+)', block)
        name = urllib.parse.unquote_plus(name_m.group(1)).strip() if name_m else ""

        detail_clean = f"{BASE}?fun=showresume&sales=adam&id={resume_id}&of=resume"
        items.append({
            "page": page_no,
            "code": code,
            "name": name,
            "age": int(age_m.group(1)) if age_m else None,
            "marital": next((x for x in ["已婚", "未婚", "離異", "鰥寡"] if x in block_text), ""),
            "hw": f"{hw_m.group(1)}cm/{hw_m.group(2)}kg" if hw_m else "",
            "flags": flags,
            "detail": detail_clean,
            "detail_clean": detail_clean,
            "summary": block_text[:220],
        })
    return items


def main():
    first = fetch_text(LIST_URL.format(page=1))
    total_pages = parse_total_pages(first)
    all_candidates = []
    seen = set()

    for page_no in range(1, total_pages + 1):
        text = first if page_no == 1 else fetch_text(LIST_URL.format(page=page_no))
        (OUT_DIR / f"list-page{page_no}.html").write_text(text, encoding="utf-8")
        for item in parse_list_page(text, page_no):
            if item["code"] in seen:
                continue
            seen.add(item["code"])
            all_candidates.append(item)
        time.sleep(0.3)

    for item in all_candidates:
        detail_html = fetch_text(item["detail_clean"])
        detail_path = OUT_DIR / f"detail-{item['code']}.html"
        detail_path.write_text(detail_html, encoding="utf-8")
        item["detailTextLength"] = len(clean_text(detail_html))
        time.sleep(0.2)

    (OUT_DIR / "candidates-list.json").write_text(
        json.dumps(all_candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    current_ids = sorted(item["code"] for item in all_candidates)
    today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    snap = _fs("GET", "candidate_snapshots/latest")
    previous_ids = set(field(snap, "ids") or []) if snap else set()
    current_id_set = set(current_ids)

    if len(current_ids) < MIN_REASONABLE_CANDIDATES:
        raise RuntimeError(f"候選人清單異常：只抓到 {len(current_ids)} 位，停止更新快照與履歷動態")
    if previous_ids and len(current_ids) < len(previous_ids) * MAX_DROP_RATIO:
        raise RuntimeError(
            f"候選人清單異常：由 {len(previous_ids)} 位掉到 {len(current_ids)} 位，停止更新快照與履歷動態"
        )

    for cid in sorted(current_id_set - previous_ids):
        write_candidate_change(today, "added", cid)
    for cid in sorted(previous_ids - current_id_set):
        write_candidate_change(today, "removed", cid)

    _fs("PATCH", "candidate_snapshots/latest", {"fields": _fields({
        "ids": current_ids,
        "updatedAt": today,
        "count": len(current_ids),
    })})
    print(f"候選人列表已更新：{len(all_candidates)} 位，頁數：{total_pages}；candidate_snapshots/latest 已同步")


if __name__ == "__main__":
    main()
