#!/usr/bin/env python3
import importlib.util
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "inspected_pdfs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("mc", ROOT / "outputs" / "match_candidates.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def safe_name(value):
    value = value or "unnamed"
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_")[:80]


def extract_text(pdf_path):
    import pdfplumber

    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            pages.append({"page": index, "text": text})
    return pages


def main():
    docs = mc.fs_get_list("hiring_progress", page_size=100)
    rows = []
    for index, doc in enumerate(docs, 1):
        file_url = mc.field(doc, "fileUrl") or ""
        file_name = mc.field(doc, "fileName") or ""
        if not file_url:
            continue

        emp = mc.field(doc, "empName") or ""
        status = mc.field(doc, "formStatus") or ""
        confirmed = mc.field(doc, "confirmedWorker") or ""
        if confirmed and confirmed not in ("", "尚未確定"):
            continue
        if status == "暫停":
            continue
        doc_id = doc["name"].split("/")[-1]
        local_pdf = OUT_DIR / f"{index:02d}_{safe_name(emp)}_{safe_name(file_name)}.pdf"

        item = {
            "index": index,
            "id": doc_id,
            "emp": emp,
            "status": status,
            "confirmedWorker": confirmed,
            "fileName": file_name,
            "pdf": str(local_pdf),
            "downloaded": False,
            "textLength": 0,
            "conditions": {},
            "preview": "",
            "looksScanned": False,
            "error": "",
        }

        try:
            if not local_pdf.exists():
                ok = mc.download_pdf(file_url, str(local_pdf))
                item["downloaded"] = ok
            else:
                item["downloaded"] = True

            if item["downloaded"]:
                pages = extract_text(local_pdf)
                full_text = "\n".join(page["text"] for page in pages)
                item["textLength"] = len(full_text.strip())
                item["conditions"] = mc.parse_conditions(full_text)
                item["preview"] = " ".join(full_text.split())[:500]
                item["looksScanned"] = item["textLength"] < 80
                text_path = local_pdf.with_suffix(".txt")
                text_path.write_text(full_text, encoding="utf-8")
                item["textPath"] = str(text_path)
        except Exception as exc:
            item["error"] = str(exc)

        rows.append(item)
        print(
            f"{index:02d}. {emp} | text={item['textLength']} | "
            f"scanned={item['looksScanned']} | conds={item['conditions']} | {file_name}"
        )

    result_path = OUT_DIR / "summary.json"
    result_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsummary: {result_path}")


if __name__ == "__main__":
    main()
