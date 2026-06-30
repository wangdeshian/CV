#!/usr/bin/env python3
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = ROOT / "outputs" / "experience_match_results.json"

spec = importlib.util.spec_from_file_location("mc", ROOT / "outputs" / "match_candidates.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def is_active_case(doc):
    confirmed = mc.field(doc, "confirmedWorker") or ""
    status = mc.field(doc, "formStatus") or ""
    file_url = mc.field(doc, "fileUrl") or ""
    if confirmed and confirmed not in ("", "尚未確定"):
        return False
    if status == "暫停":
        return False
    if not file_url:
        return False
    return True


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


def main():
    active_ids = get_active_candidate_ids()
    results = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    rec_by_emp = {}
    rec_by_id = {}
    for row in results:
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
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for doc in docs:
        emp = mc.field(doc, "empName") or ""
        doc_id = doc["name"].split("/")[-1]
        if not is_active_case(doc):
            continue
        current_value = mc.field(doc, "targetWorker") or ""
        current_active_codes = [code for code in extract_candidate_ids(current_value) if code in active_ids]
        new_codes = rec_by_id.get(doc_id) or rec_by_emp.get(emp) or []
        merged_codes = []
        for code in current_active_codes + new_codes:
            if code in active_ids and code not in merged_codes:
                merged_codes.append(code)
        value = " ".join(merged_codes)
        if not value:
            if current_value.strip():
                body = {
                    "fields": {
                        "targetWorker": {"stringValue": ""},
                        "updatedAt": {"timestampValue": now},
                    }
                }
                mc._fs("PATCH", f"hiring_progress/{doc_id}?updateMask.fieldPaths=targetWorker&updateMask.fieldPaths=updatedAt", body)
                updated.append({"emp": emp, "id": doc_id, "targetWorker": "", "removedInactiveOnly": True})
                print(f"CLEARED {emp}: removed inactive targetWorker ids")
                continue
            skipped.append({"emp": emp, "id": doc_id, "reason": "沒有可回填推薦名單"})
            continue

        body = {
            "fields": {
                "targetWorker": {"stringValue": value},
                "updatedAt": {"timestampValue": now},
            }
        }
        mc._fs("PATCH", f"hiring_progress/{doc_id}?updateMask.fieldPaths=targetWorker&updateMask.fieldPaths=updatedAt", body)
        updated.append({"emp": emp, "id": doc_id, "targetWorker": value})
        print(f"UPDATED {emp}: {value}")

    out = ROOT / "outputs" / "backfill_target_worker_result.json"
    out.write_text(json.dumps({"updated": updated, "skipped": skipped}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"result: {out}")


if __name__ == "__main__":
    main()
