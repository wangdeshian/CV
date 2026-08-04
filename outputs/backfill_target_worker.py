#!/usr/bin/env python3
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = ROOT / "outputs" / "experience_match_results.json"
CANDIDATE_LIST = ROOT / "work" / "imaid" / "candidates-list.json"
MAX_DESIRED_AGE_GAP = 8
DRY_RUN = os.environ.get("DRY_RUN") == "1"

spec = importlib.util.spec_from_file_location("mc", ROOT / "outputs" / "match_candidates.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def patch_case(doc_id, fields, update_masks):
    """統一走這裡，DRY_RUN=1 時只印出不寫入。"""
    mask_query = "&".join(f"updateMask.fieldPaths={name}" for name in update_masks)
    if DRY_RUN:
        print(f"  [DRY_RUN] 略過寫入 {doc_id}（{', '.join(update_masks)}）")
        return
    mc._fs("PATCH", f"hiring_progress/{doc_id}?{mask_query}", {"fields": fields})


def case_is_open(doc):
    """尚未確定人選、未暫停、未封存。"""
    confirmed = mc.field(doc, "confirmedWorker") or ""
    status = mc.field(doc, "formStatus") or ""
    if confirmed and confirmed not in ("", "尚未確定"):
        return False
    if status == "暫停":
        return False
    if mc.field(doc, "isArchived"):
        return False
    return True


def is_active_case(doc):
    """會進入自動配對的案件：未確定人選、未暫停、未封存、且有聘工表。"""
    if not case_is_open(doc):
        return False
    return bool(mc.field(doc, "fileUrl") or "")


def get_active_candidate_ids():
    snap = mc._fs("GET", "candidate_snapshots/latest")
    return set(mc.field(snap, "ids") or [])


def normalize_candidate_id(value):
    m = re.fullmatch(r"([A-Z]+)\.?(\d+)", value.strip())
    if not m:
        return value.strip()
    return f"{m.group(1)}.{m.group(2)}"


def extract_candidate_ids(value):
    return [normalize_candidate_id(x) for x in re.findall(r"[A-Z]+\.?\d+", value or "")]


def load_candidate_ages():
    if not CANDIDATE_LIST.exists():
        return {}
    candidates = json.loads(CANDIDATE_LIST.read_text(encoding="utf-8"))
    return {item.get("code"): item.get("age") for item in candidates if item.get("code")}


def age_allowed(candidate_id, desired_age, candidate_ages):
    if not desired_age:
        return True
    age = candidate_ages.get(candidate_id)
    if not age:
        return False
    return abs(age - desired_age) <= MAX_DESIRED_AGE_GAP


def fs_value_to_python(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return value["booleanValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "arrayValue" in value:
        return [fs_value_to_python(item) for item in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {
            key: fs_value_to_python(item)
            for key, item in value["mapValue"].get("fields", {}).items()
        }
    return None


def doc_array_field(doc, key):
    if not doc or "fields" not in doc:
        return []
    value = doc["fields"].get(key)
    if not value:
        return []
    parsed = fs_value_to_python(value)
    return parsed if isinstance(parsed, list) else []


def to_fs_value(value):
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [to_fs_value(item) for item in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {key: to_fs_value(item) for key, item in value.items()}}}
    if value is None:
        return {"nullValue": "NULL_VALUE"}
    return {"stringValue": str(value)}


def build_history_entry(now, local_date, emp, doc_id, added_codes, recommended_codes, merged_codes, demand):
    return {
        "runAt": now,
        "date": local_date,
        "source": "auto-match",
        "employer": emp,
        "caseId": doc_id,
        "addedCandidates": added_codes,
        "recommendedCandidates": recommended_codes,
        "targetWorkerAfter": merged_codes,
        "desiredAgeMin": demand.get("desiredAgeMin"),
        "desiredHeightMin": demand.get("desiredHeightMin"),
        "desiredWeightMin": demand.get("desiredWeightMin"),
    }


def auto_recommended_codes(history):
    codes = set()
    for entry in history:
        if not isinstance(entry, dict):
            continue
        for code in entry.get("recommendedCandidates") or []:
            codes.add(normalize_candidate_id(str(code)))
        for code in entry.get("addedCandidates") or []:
            codes.add(normalize_candidate_id(str(code)))
    return codes


def history_key(entry):
    return (
        entry.get("date") or "",
        tuple(entry.get("recommendedCandidates") or []),
        tuple(entry.get("targetWorkerAfter") or []),
    )


def dedupe_history(history):
    seen = set()
    deduped = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        key = history_key(entry)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def main():
    active_ids = get_active_candidate_ids()
    candidate_ages = load_candidate_ages()
    results = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    rec_by_emp = {}
    rec_by_id = {}
    demand_by_emp = {}
    demand_by_id = {}
    skipped_auto_match_emp = set()
    skipped_auto_match_id = set()
    for row in results:
        if row.get("status") == "skipped_factory_or_care_facility":
            skipped_auto_match_emp.add(row["employer"])
            if row.get("id"):
                skipped_auto_match_id.add(row["id"])
            continue
        demand = row.get("demand") or {}
        demand_by_emp[row["employer"]] = demand
        if row.get("id"):
            demand_by_id[row["id"]] = demand
        recs = row.get("recommendations") or []
        if not recs:
            continue
        codes = [r["candidateId"] for r in recs if r.get("candidateId") in active_ids]
        if codes:
            rec_by_emp[row["employer"]] = codes
            if row.get("id"):
                rec_by_id[row["id"]] = codes

    docs = mc.fs_get_list("hiring_progress", page_size=100)
    updated = []
    skipped = []
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat().replace("+00:00", "Z")
    local_date = now_dt.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()

    for doc in docs:
        emp = mc.field(doc, "empName") or ""
        doc_id = doc["name"].split("/")[-1]
        current_value = mc.field(doc, "targetWorker") or ""
        if not is_active_case(doc):
            # 沒有聘工表的案件不做配對，但已下架的編號還是要清掉。
            # 暫停、已封存、已確定人選的案件完全不碰。
            if case_is_open(doc) and current_value.strip():
                kept = [c for c in extract_candidate_ids(current_value) if c in active_ids]
                value = " ".join(kept)
                if value != current_value.strip():
                    patch_case(
                        doc_id,
                        {
                            "targetWorker": {"stringValue": value},
                            "updatedAt": {"timestampValue": now},
                        },
                        ["targetWorker", "updatedAt"],
                    )
                    updated.append({"emp": emp, "id": doc_id, "targetWorker": value, "cleanupOnly": True})
                    print(f"CLEANED {emp}: 無聘工表，僅移除已下架編號 -> {value or '(清空)'}")
            continue
        if doc_id in skipped_auto_match_id or emp in skipped_auto_match_emp:
            history = dedupe_history(doc_array_field(doc, "recommendationHistory"))
            auto_codes = auto_recommended_codes(history)
            current_codes = extract_candidate_ids(current_value)
            remaining_codes = [code for code in current_codes if code not in auto_codes]
            value = " ".join(remaining_codes)
            if value != current_value.strip() or history != doc_array_field(doc, "recommendationHistory"):
                patch_case(
                    doc_id,
                    {
                        "targetWorker": {"stringValue": value},
                        "recommendationHistory": to_fs_value(history[-100:]),
                        "updatedAt": {"timestampValue": now},
                    },
                    ["targetWorker", "recommendationHistory", "updatedAt"],
                )
                updated.append({"emp": emp, "id": doc_id, "targetWorker": value, "skippedAutoMatch": True})
                print(f"SKIPPED {emp}: factory/care facility, removed auto-match targetWorker ids")
            else:
                skipped.append({"emp": emp, "id": doc_id, "reason": "工廠或護理之家不自動配對"})
            continue
        demand = demand_by_id.get(doc_id) or demand_by_emp.get(emp) or {}
        desired_age = demand.get("desiredAgeMin")
        existing_history = dedupe_history(doc_array_field(doc, "recommendationHistory"))
        auto_codes = auto_recommended_codes(existing_history)
        # 已下架的一律移除；年齡規則只套用在自動配對放上去的編號，
        # 手動輸入的編號不因年齡落差被刪掉。
        current_active_codes = []
        for code in extract_candidate_ids(current_value):
            if code not in active_ids:
                continue
            if code in auto_codes and not age_allowed(code, desired_age, candidate_ages):
                continue
            current_active_codes.append(code)
        new_codes = rec_by_id.get(doc_id) or rec_by_emp.get(emp) or []
        merged_codes = []
        for code in current_active_codes + new_codes:
            if code in active_ids and code not in merged_codes:
                merged_codes.append(code)
        added_codes = [code for code in new_codes if code not in current_active_codes]
        value = " ".join(merged_codes)
        if not value:
            if current_value.strip():
                patch_case(
                    doc_id,
                    {
                        "targetWorker": {"stringValue": ""},
                        "updatedAt": {"timestampValue": now},
                    },
                    ["targetWorker", "updatedAt"],
                )
                updated.append({"emp": emp, "id": doc_id, "targetWorker": "", "removedInactiveOnly": True})
                print(f"CLEARED {emp}: removed inactive targetWorker ids")
                continue
            skipped.append({"emp": emp, "id": doc_id, "reason": "沒有可回填推薦名單"})
            continue

        fields = {
            "targetWorker": {"stringValue": value},
            "updatedAt": {"timestampValue": now},
        }
        update_masks = ["targetWorker", "updatedAt"]
        if new_codes:
            original_history = doc_array_field(doc, "recommendationHistory")
            history = list(existing_history)
            entry = build_history_entry(now, local_date, emp, doc_id, added_codes, new_codes, merged_codes, demand)
            if history_key(entry) not in {history_key(item) for item in history}:
                history.append(entry)
            if history != original_history:
                fields["recommendationHistory"] = to_fs_value(history[-100:])
                update_masks.append("recommendationHistory")

        if value == current_value.strip() and "recommendationHistory" not in update_masks:
            # 內容沒有變化就不要寫，免得每天把 updatedAt 洗掉、看不出真正的異動。
            skipped.append({"emp": emp, "id": doc_id, "reason": "推薦名單與現況相同，未變更"})
            print(f"UNCHANGED {emp}: {value}")
            continue

        patch_case(doc_id, fields, update_masks)
        updated.append({"emp": emp, "id": doc_id, "targetWorker": value, "addedCandidates": added_codes})
        print(f"UPDATED {emp}: {value}")

    out = ROOT / "outputs" / "backfill_target_worker_result.json"
    out.write_text(json.dumps({"updated": updated, "skipped": skipped}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"result: {out}")


if __name__ == "__main__":
    main()
