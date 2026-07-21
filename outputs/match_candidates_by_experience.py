#!/usr/bin/env python3
"""
以聘工表照護內容 + 移工過去經驗做配對。

資料來源：
- outputs/inspected_pdfs/*.txt：聘工表文字抽取結果
- work/imaid/candidates-list.json：候選人清單
- work/imaid/detail-*.html：候選人履歷詳細頁

注意：聘工表的固定表格選項會被 pdfplumber 全部抽成文字，無法可靠分辨是否勾選。
本腳本只把病人資料、明確填寫欄位、自由文字與可推估照護需求作為高權重依據。
"""

import html
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_LIST = ROOT / "work" / "imaid" / "candidates-list.json"
DETAIL_DIR = ROOT / "work" / "imaid"
PDF_TEXT_DIR = ROOT / "outputs" / "inspected_pdfs"
RESULT_PATH = ROOT / "outputs" / "experience_match_results.json"
PDF_SUMMARY = PDF_TEXT_DIR / "summary.json"
MAX_DESIRED_AGE_GAP = 8
ASSIGNED_CANDIDATE_BLOCKLIST = {
    "SN.462",
    "SN.459",
    "SN.480",
    "SJ.236",
    "SJ.207",
    "TB.7280",
    "MKB.2680",
    "CL.327",
}
SKIP_AUTO_MATCH_KEYWORDS = [
    "工廠", "工業", "作業員", "產線", "包裝員", "製造",
    "豆腐店",
    "護理之家", "養護", "長照", "安養", "照護中心",
]

spec = importlib.util.spec_from_file_location("mc", ROOT / "outputs" / "match_candidates.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

CARE_KEYWORDS = {
    "elder": ["照顧老人", "老人", "奶奶", "爺爺", "阿公", "阿嬤", "grandma", "grandpa"],
    "housework": ["家務事", "所有家務", "家庭清潔", "打掃", "清潔"],
    "cooking": ["煮飯", "三餐", "做飯"],
    "laundry": ["洗衫", "洗衣"],
    "ironing": ["燙衫", "燙衣"],
    "bathing": ["洗澡"],
    "night": ["夜間", "晚上"],
    "mobility": ["扶持", "行走", "無法行走", "抱", "移位", "翻身", "拍背", "坐輪椅"],
    "medical": ["糖尿病", "量血糖", "吃藥", "導尿", "鼻胃管", "中風", "失智", "老人癡呆"],
    "child": ["照顧小孩", "小孩", "嬰兒"],
}

TASK_LABEL = {
    "elder": "老人照護",
    "housework": "家務清潔",
    "cooking": "煮飯三餐",
    "laundry": "洗衣",
    "ironing": "燙衣",
    "bathing": "洗澡協助",
    "night": "夜間照顧",
    "mobility": "扶行/移位/翻身",
    "medical": "疾病或醫療照護",
    "child": "小孩照顧",
}


def clean_text(value):
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_hw(hw):
    m = re.search(r"(\d{3})\s*cm\s*/\s*(\d{2,3})\s*kg", hw or "", re.I)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def get_active_candidate_ids():
    snap = mc._fs("GET", "candidate_snapshots/latest")
    return set(mc.field(snap, "ids") or [])


def keyword_hits(text):
    hits = set()
    for key, words in CARE_KEYWORDS.items():
        if any(w.lower() in text.lower() for w in words):
            hits.add(key)
    return hits


def skip_auto_match_reason(employer, text):
    target = f"{employer or ''} {text or ''}"
    for keyword in SKIP_AUTO_MATCH_KEYWORDS:
        if keyword in target:
            if keyword in {"護理之家", "養護", "長照", "安養", "照護中心"}:
                return "護理之家／養護機構案件不自動特別配對"
            return "工廠類案件不自動特別配對"
    return ""


def parse_candidate_detail(code):
    path = DETAIL_DIR / f"detail-{code}.html"
    if not path.exists():
        return "", set(), []
    raw = path.read_text(encoding="utf-8", errors="ignore")
    text = clean_text(raw)
    hits = keyword_hits(text)
    snippets = []
    for word in ["工作經驗", "備註", "照顧", "家務事", "煮飯", "洗衫", "洗澡", "老人"]:
        i = text.find(word)
        if i >= 0:
            snippets.append(text[max(0, i - 80): i + 220])
    return text, hits, snippets[:3]


def load_candidates(active_ids=None):
    candidates = json.loads(CANDIDATE_LIST.read_text(encoding="utf-8"))
    result = []
    seen = set()
    for c in candidates:
        code = c.get("code") or ""
        if active_ids and code not in active_ids:
            continue
        if code in ASSIGNED_CANDIDATE_BLOCKLIST:
            continue
        if not code or code in seen:
            continue
        seen.add(code)
        height, weight = parse_hw(c.get("hw", ""))
        detail_text, exp_hits, snippets = parse_candidate_detail(code)
        result.append({
            "code": code,
            "name": c.get("name") or "",
            "age": c.get("age"),
            "height": height,
            "weight": weight,
            "flags": c.get("flags") or [],
            "detailExists": bool(detail_text),
            "experienceTags": sorted(exp_hits),
            "experienceSnippets": snippets,
        })
    return result


def parse_employer_text(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    compact = re.sub(r"\s+", " ", text)
    patient_age = None
    patient_height = None
    patient_weight = None

    m = re.search(r"年齡\s*UMUR[：: ]*(\d{1,3})\s*歲", compact)
    if m:
        patient_age = int(m.group(1))
    m = re.search(r"身高[：: ]*(\d{3})\s*CM", compact, re.I)
    if m:
        patient_height = int(m.group(1))
    m = re.search(r"體重[：: ]*(\d{2,3})\s*KG", compact, re.I)
    if m:
        patient_weight = int(m.group(1))

    desired_age = None
    desired_height = None
    desired_weight = None
    i = compact.find("期望女傭條件")
    desired_block = compact[i:i + 900] if i >= 0 else compact
    m = re.search(r"年齡\s*UMUR\s*(\d{2})\s*歲", desired_block)
    if m:
        desired_age = int(m.group(1))
    m = re.search(r"身高\s*Tinggi\s*(\d{3})\s*cm", desired_block, re.I)
    if m:
        desired_height = int(m.group(1))
    m = re.search(r"體重\s*Berat\s*(\d{2,3})\s*kg", desired_block, re.I)
    if m:
        desired_weight = int(m.group(1))

    special = ""
    s = compact.find("外勞工作內容說明")
    if s >= 0:
        e_candidates = [
            compact.find("確工女傭編號", s),
            compact.find("來台工作地點", s),
        ]
        e_candidates = [x for x in e_candidates if x > s]
        e = min(e_candidates) if e_candidates else s + 700
        special = compact[s:e]
    special_hits = keyword_hits(special)
    # Template forms contain all checkbox labels. Use common elder-care demand as low-risk inference
    # only when the patient is elderly or has mobility-sensitive body size.
    inferred = set()
    if patient_age and patient_age >= 70:
        inferred.update(["elder", "housework", "cooking", "laundry"])
    if patient_weight and patient_weight >= 55:
        inferred.add("mobility")

    care_demand = inferred | special_hits

    return {
        "sourceText": str(path),
        "employer": re.sub(r"^\d+_|_.*$", "", path.stem),
        "patientAge": patient_age,
        "patientHeight": patient_height,
        "patientWeight": patient_weight,
        "desiredAgeMin": desired_age,
        "desiredHeightMin": desired_height,
        "desiredWeightMin": desired_weight,
        "rawTextLength": len(compact.strip()),
        "textExtracted": len(compact.strip()) >= 80,
        "specialWorkText": special[:500],
        "explicitTags": sorted(special_hits),
        "careDemandTags": sorted(care_demand),
        "skipAutoMatchReason": skip_auto_match_reason(path.stem, compact),
    }


def score_candidate(candidate, demand):
    score = 0
    reasons = []
    risks = []
    demand_tags = set(demand["careDemandTags"])
    exp_tags = set(candidate["experienceTags"])

    desired_age = demand.get("desiredAgeMin")
    candidate_age = candidate.get("age")
    if desired_age and candidate_age:
        age_gap = abs(candidate_age - desired_age)
        if age_gap > MAX_DESIRED_AGE_GAP:
            return -999, [], [f"年齡 {candidate_age} 歲與聘工表期望 {desired_age} 歲落差 {age_gap} 歲，已排除"]

    if not candidate["detailExists"]:
        risks.append("缺候選人詳細履歷，僅能用基本資料")

    matched = sorted(demand_tags & exp_tags)
    for tag in matched:
        score += 8
        reasons.append(f"經驗符合：{TASK_LABEL[tag]}")

    if "elder" in demand_tags and "elder" not in exp_tags:
        score -= 6
        risks.append("未看到明確老人照護經驗")
    if "mobility" in demand_tags:
        if "mobility" in exp_tags:
            score += 6
        elif candidate.get("weight") and candidate["weight"] >= 55:
            score += 2
            reasons.append("體格可支援扶行類工作，仍需確認實務經驗")
        else:
            score -= 3
            risks.append("扶行/移位需求需確認體力與經驗")

    if desired_age and candidate_age:
        age_gap = abs(candidate_age - desired_age)
        if age_gap <= 2:
            score += 8
        elif age_gap <= 5:
            score += 5
        else:
            score += 2
        reasons.append(f"年齡接近聘工表期望 {desired_age} 歲（候選人 {candidate_age} 歲）")

    if demand.get("desiredHeightMin") and candidate.get("height"):
        if candidate["height"] >= demand["desiredHeightMin"]:
            score += 3
            reasons.append(f"身高符合至少 {demand['desiredHeightMin']} cm")
        else:
            score -= 3
            risks.append(f"身高低於期望 {demand['desiredHeightMin']} cm")

    if demand.get("desiredWeightMin") and candidate.get("weight"):
        if candidate["weight"] >= demand["desiredWeightMin"]:
            score += 2
            reasons.append(f"體重符合至少 {demand['desiredWeightMin']} kg")
        else:
            score -= 2
            risks.append(f"體重低於期望 {demand['desiredWeightMin']} kg")

    if any("Taiwan" in f for f in candidate["flags"]):
        score += 3
        reasons.append("有台灣工作經驗")
    elif any(place in " ".join(candidate["flags"]) for place in ["Singapore", "Hong Kong"]):
        score += 2
        reasons.append("有海外家庭照護/家務經驗")

    if not reasons:
        reasons.append("無明確經驗符合點，排序可靠度低")

    return score, reasons, risks


def main():
    active_ids = get_active_candidate_ids()
    candidates = load_candidates(active_ids)
    print(f"目前在架候選人：{len(active_ids)}，本機可評分履歷：{len(candidates)}")
    active_by_text = {}
    if PDF_SUMMARY.exists():
        summary = json.loads(PDF_SUMMARY.read_text(encoding="utf-8"))
        for item in summary:
            confirmed = item.get("confirmedWorker") or ""
            status = item.get("status") or ""
            if confirmed and confirmed not in ("", "尚未確定"):
                continue
            if status == "暫停":
                continue
            text_path = item.get("textPath")
            if text_path:
                active_by_text[str(Path(text_path))] = item

    pdf_texts = sorted(PDF_TEXT_DIR.glob("*.txt"))
    results = []
    for path in pdf_texts:
        if active_by_text and str(path.resolve()) not in active_by_text:
            continue
        demand = parse_employer_text(path)
        source_item = active_by_text.get(str(path.resolve())) if active_by_text else None
        if source_item:
            demand["id"] = source_item.get("id")
            demand["employer"] = source_item.get("emp") or demand["employer"]
            demand["skipAutoMatchReason"] = skip_auto_match_reason(demand["employer"], demand.get("specialWorkText") or "")
        if not demand["textExtracted"]:
            results.append({
                "id": demand.get("id"),
                "employer": demand["employer"],
                "sourceText": demand["sourceText"],
                "status": "needs_ocr",
                "reason": "PDF 文字抽取為空，需 OCR 或人工查看後才能依工作內容配對",
                "recommendations": [],
            })
            continue
        if demand.get("skipAutoMatchReason"):
            results.append({
                "id": demand.get("id"),
                "employer": demand["employer"],
                "sourceText": demand["sourceText"],
                "status": "skipped_factory_or_care_facility",
                "reason": demand["skipAutoMatchReason"],
                "demand": demand,
                "recommendations": [],
            })
            continue

        scored = []
        for c in candidates:
            score, reasons, risks = score_candidate(c, demand)
            scored.append({
                "score": score,
                "candidateId": c["code"],
                "name": c["name"],
                "age": c["age"],
                "height": c["height"],
                "weight": c["weight"],
                "flags": c["flags"],
                "reasons": reasons[:5],
                "risks": risks[:3],
                "experienceSnippets": c["experienceSnippets"][:2],
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        top5 = [x for x in scored if x["score"] > 0][:5]
        status = "matched" if demand["careDemandTags"] and top5 else "criteria_only" if top5 else "no_reliable_match"
        results.append({
            "id": demand.get("id"),
            "employer": demand["employer"],
            "sourceText": demand["sourceText"],
            "status": status,
            "demand": demand,
            "recommendations": top5,
        })

    RESULT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in results:
        print(f"\n▶ {row['employer']} [{row['status']}]")
        if row.get("demand"):
            tags = ", ".join(TASK_LABEL.get(t, t) for t in row["demand"]["careDemandTags"])
            print(f"  需求推估: {tags or '未抓到'}")
        if row["recommendations"]:
            for i, rec in enumerate(row["recommendations"], 1):
                print(f"  {i}. {rec['candidateId']} score={rec['score']} - {'；'.join(rec['reasons'][:3])}")
        else:
            print(f"  {row.get('reason', '無可靠推薦')}")
    print(f"\n結果已輸出：{RESULT_PATH}")


if __name__ == "__main__":
    main()
