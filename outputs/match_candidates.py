#!/usr/bin/env python3
"""
雇主選工配對腳本
1. 從 Firestore 取得尚未確定人選的雇主
2. 下載各雇主聘工表 PDF，以 OCR 解析條件
3. 與 candidate_snapshots 裡的候選人配對，推薦 5 人
"""

import re, json, sys, os, tempfile
import urllib.request, urllib.error

FIREBASE_PROJECT = "adamcv-79d47"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyAB-b45xKSpUg5I43NUxZtzjytj9p5Fu_A")
FIRESTORE_BASE   = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"
RESULT_PATH = os.path.join(os.path.dirname(__file__), "match_candidates_result.json")

COUNTRY_MAP = {
    "SJ": "印尼", "SN": "印尼", "IN": "印尼",
    "MKB": "菲律賓", "KB": "菲律賓", "CL": "菲律賓", "PH": "菲律賓",
    "TB": "泰國", "TH": "泰國",
    "VN": "越南", "VT": "越南",
    "MM": "緬甸",
}

_ID_TOKEN = None
_ID_TOKEN_TRIED = False

def get_id_token():
    """匿名登入取得 idToken，讓 Firestore 規則可以要求 request.auth != null。

    取不到 token 時回傳 None 並繼續，避免規則尚未收緊前流程直接中斷。
    """
    global _ID_TOKEN, _ID_TOKEN_TRIED
    if _ID_TOKEN or _ID_TOKEN_TRIED:
        return _ID_TOKEN
    _ID_TOKEN_TRIED = True
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
    body = json.dumps({"returnSecureToken": True}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            _ID_TOKEN = json.loads(r.read()).get("idToken")
        print("匿名登入成功，寫入將帶 Firebase 驗證")
    except Exception as e:
        print(f"匿名登入失敗，改用未驗證模式：{e}")
    return _ID_TOKEN

def _fs(method, path, body=None):
    sep = "&" if "?" in path else "?"
    url  = f"{FIRESTORE_BASE}/{path}{sep}key={FIREBASE_API_KEY}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    token = get_id_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req  = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404: return None
        raise

def fs_get_list(col, page_size=50):
    r = _fs("GET", f"{col}?pageSize={page_size}")
    return r.get("documents", []) if r else []

def field(doc, key):
    if not doc or "fields" not in doc: return None
    f = doc["fields"].get(key, {})
    if "stringValue"   in f: return f["stringValue"]
    if "integerValue"  in f: return int(f["integerValue"])
    if "booleanValue"  in f: return f["booleanValue"]
    if "arrayValue"    in f:
        return [v.get("stringValue","") for v in f["arrayValue"].get("values",[])]
    return None

def download_pdf(url, dest_path):
    try:
        urllib.request.urlretrieve(url, dest_path)
        return True
    except Exception as e:
        print(f"  ⚠ PDF 下載失敗: {e}")
        return False

def extract_text_from_pdf(pdf_path):
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                text += t + "\n"
        if text.strip():
            return text
    except Exception as e:
        print(f"  pdfplumber failed: {e}")

    try:
        from pdf2image import convert_from_path
        import pytesseract
        images = convert_from_path(pdf_path, dpi=200)
        for img in images:
            text += pytesseract.image_to_string(img, lang="chi_tra+eng") + "\n"
    except Exception as e:
        print(f"  OCR failed: {e}")

    return text

def parse_conditions(text):
    conds = {}
    for nation in ["印尼", "菲律賓", "越南", "泰國", "緬甸"]:
        if nation in text:
            conds.setdefault("countries", []).append(nation)
    age_m = re.search(r'(\d{2})\s*[-至~～]\s*(\d{2})\s*歲?', text)
    if age_m:
        conds["age_min"] = int(age_m.group(1))
        conds["age_max"] = int(age_m.group(2))
    if "未婚" in text:
        conds["marital"] = "未婚"
    elif "已婚" in text:
        conds["marital"] = "已婚"
    h_m = re.search(r'(\d{3})\s*cm?\s*以上', text)
    if h_m:
        conds["height_min"] = int(h_m.group(1))
    return conds

def get_all_candidates():
    snap = _fs("GET", "candidate_snapshots/latest")
    ids  = field(snap, "ids") or []
    candidates = []
    for cid in ids:
        prefix = re.match(r'^([A-Z]+)\.', cid)
        p = prefix.group(1) if prefix else ""
        candidates.append({
            "candidateId": cid,
            "country": COUNTRY_MAP.get(p, p),
        })
    return candidates

def score_candidate(c, conds):
    score = 0
    reasons = []
    wanted = conds.get("countries", [])
    if wanted:
        if c["country"] in wanted:
            score += 10
            reasons.append(f"國籍符合（{c['country']}）")
        else:
            score -= 5
    if c.get("age") and (conds.get("age_min") or conds.get("age_max")):
        try:
            age = int(c["age"])
            if conds.get("age_min",0) <= age <= conds.get("age_max",99):
                score += 5
                reasons.append(f"年齡符合（{age}歲）")
            else:
                score -= 3
        except: pass
    if c.get("marital") and conds.get("marital"):
        if c["marital"] == conds["marital"]:
            score += 3
        else:
            score -= 2
    if c.get("height") and conds.get("height_min"):
        try:
            if int(c["height"]) >= conds["height_min"]:
                score += 2
            else:
                score -= 1
        except: pass
    return score, reasons

def match_top5(candidates, conds):
    scored = [(s, c, r) for c in candidates
              for s, r in [score_candidate(c, conds)] if s > 0]
    scored.sort(key=lambda x: -x[0])
    return scored[:5]

def main():
    print("\n=== 雇主選工配對系統 ===\n")
    candidates = get_all_candidates()
    print(f"► 在架候選人：{len(candidates)} 位\n")

    docs = fs_get_list("hiring_progress")
    targets = []
    for doc in docs:
        emp    = field(doc, "empName") or ""
        status = field(doc, "formStatus") or ""
        cw     = field(doc, "confirmedWorker") or ""
        furl   = field(doc, "fileUrl") or ""
        doc_id = doc["name"].split("/")[-1]
        if cw and cw not in ("", "尚未確定"): continue
        if status == "暫停": continue
        if not furl: continue
        targets.append({"id": doc_id, "emp": emp, "status": status, "fileUrl": furl})

    print(f"► 尚未確定人選：{len(targets)} 件\n")

    results = []
    for t in targets:
        print(f"▶ {t['emp']} [{t['status']}]")
        conds = {}
        if t["fileUrl"]:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
            if download_pdf(t["fileUrl"], tmp_path):
                text  = extract_text_from_pdf(tmp_path)
                conds = parse_conditions(text)
                print(f"  條件: {conds}")
                os.unlink(tmp_path)

        top5 = match_top5(candidates, conds)
        for rank, (score, c, reasons) in enumerate(top5, 1):
            print(f"  {rank}. {c['candidateId']} ({c['country']}) — {' | '.join(reasons) or '依國籍篩選'}")
        if not top5:
            print("  （無符合條件的候選人）")
        results.append({
            "id": t["id"],
            "emp": t["emp"],
            "status": t["status"],
            "conditions": conds,
            "recommendations": [
                {
                    "rank": rank,
                    "score": score,
                    "candidateId": c["candidateId"],
                    "country": c["country"],
                    "reasons": reasons,
                }
                for rank, (score, c, reasons) in enumerate(top5, 1)
            ],
        })
        print()

    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"► 結果已輸出：{RESULT_PATH}")

if __name__ == "__main__":
    main()
