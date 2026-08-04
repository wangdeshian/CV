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


MIN_TEXT_LENGTH = 80


def extract_text(pdf_path):
    import pdfplumber

    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            pages.append({"page": index, "text": text})
    return pages


def ocr_text(pdf_path):
    """掃描型聘工表用 OCR 補救。需要 poppler-utils 與 tesseract-ocr-chi-tra。"""
    from pdf2image import convert_from_path
    import pytesseract

    pages = []
    for index, image in enumerate(convert_from_path(str(pdf_path), dpi=200), 1):
        pages.append({"page": index, "text": pytesseract.image_to_string(image, lang="chi_tra+eng")})
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
        if mc.field(doc, "isArchived"):
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
            "usedOcr": False,
            "ocrError": "",
            "error": "",
        }

        try:
            # 每次重抓，避免雇主換了聘工表還沿用舊檔。
            item["downloaded"] = mc.download_pdf(file_url, str(local_pdf))

            if item["downloaded"]:
                pages = extract_text(local_pdf)
                full_text = "\n".join(page["text"] for page in pages)
                item["looksScanned"] = len(full_text.strip()) < MIN_TEXT_LENGTH

                if item["looksScanned"]:
                    # 掃描圖型 PDF：pdfplumber 抽不到字，改走 OCR。
                    try:
                        ocr_pages = ocr_text(local_pdf)
                        ocr_full = "\n".join(page["text"] for page in ocr_pages)
                        if len(ocr_full.strip()) >= MIN_TEXT_LENGTH:
                            full_text = ocr_full
                            item["usedOcr"] = True
                    except Exception as ocr_exc:
                        item["ocrError"] = str(ocr_exc)

                item["textLength"] = len(full_text.strip())
                item["conditions"] = mc.parse_conditions(full_text)
                item["preview"] = " ".join(full_text.split())[:500]
                text_path = local_pdf.with_suffix(".txt")
                text_path.write_text(full_text, encoding="utf-8")
                item["textPath"] = str(text_path)
        except Exception as exc:
            # 抽取失敗要當成「讀不到」，否則會被誤認為正常的空表格。
            item["error"] = str(exc)
            item["looksScanned"] = True

        rows.append(item)
        print(
            f"{index:02d}. {emp} | text={item['textLength']} | "
            f"scanned={item['looksScanned']} | ocr={item['usedOcr']} | "
            f"conds={item['conditions']} | {file_name}"
            + (f" | ERROR={item['error']}" if item["error"] else "")
            + (f" | OCR_ERROR={item['ocrError']}" if item["ocrError"] else "")
        )

    result_path = OUT_DIR / "summary.json"
    result_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    ocr_used = [r["emp"] for r in rows if r.get("usedOcr")]
    still_blank = [r["emp"] for r in rows if r["textLength"] < MIN_TEXT_LENGTH]
    if ocr_used:
        print(f"\nOCR 補救成功：{', '.join(ocr_used)}")
    if still_blank:
        print(f"仍讀不到內容、需要人工補資料：{', '.join(still_blank)}")
    print(f"\nsummary: {result_path}")


if __name__ == "__main__":
    main()
