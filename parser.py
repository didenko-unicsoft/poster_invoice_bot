import os
import io
import re
import json
import math
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    # HEIC support not available; continue without it
    pass

from typing import Dict, Any, List, Optional
from datetime import datetime

# OpenAI (optional)
OPENAI_AVAILABLE = True
try:
    from openai import OpenAI
except Exception:
    OPENAI_AVAILABLE = False

ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".xls", ".xlsx", ".csv"}

def _extract_text_from_pdf(pdf_path: str) -> str:
    text_parts = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            t = page.get_text("text", flags=1+2+8)  # text with ligatures, preserve
            if t:
                text_parts.append(t)
    return "\n".join(text_parts).strip()

def _ocr_pdf_as_images(pdf_path: str) -> str:
    text_parts = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            t = pytesseract.image_to_string(img)
            if t:
                text_parts.append(t)
    return "\n".join(text_parts).strip()

def _ocr_image(img_path: str) -> str:
    img = Image.open(img_path)
    return pytesseract.image_to_string(img)

def _parse_excel_csv(path: str) -> Dict[str, Any]:
    import pandas as pd
    items = []
    try:
        if path.lower().endswith(".csv"):
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)
        # Heuristic columns
        cols = {c.lower(): c for c in df.columns}
        name_col = next((cols[c] for c in cols if "name" in c or "товар" in c or "опис" in c), None)
        qty_col = next((cols[c] for c in cols if "qty" in c or "кільк" in c or "quantity" in c), None)
        price_col = next((cols[c] for c in cols if "price" in c or "ціна" in c), None)
        uom_col = next((cols[c] for c in cols if "uom" in c or "од" in c or "unit" in c), None)
        tax_col = next((cols[c] for c in cols if "tax" in c or "пдв" in c or "vat" in c), None)
        barcode_col = next((cols[c] for c in cols if "barcode" in c or "штрих" in c), None)
        sku_col = next((cols[c] for c in cols if "sku" in c or "артикул" in c), None)

        for _, row in df.iterrows():
            item = {
                "name": (str(row[name_col]) if name_col else "").strip(),
                "quantity": float(row[qty_col]) if qty_col and not pd.isna(row[qty_col]) else 0.0,
                "uom": (str(row[uom_col]) if uom_col else "").strip() or None,
                "price": float(row[price_col]) if price_col and not pd.isna(row[price_col]) else 0.0,
                "tax": float(row[tax_col]) if tax_col and not pd.isna(row[tax_col]) else None,
                "barcode": (str(row[barcode_col]).strip() if barcode_col and not pd.isna(row[barcode_col]) else None),
                "sku": (str(row[sku_col]).strip() if sku_col and not pd.isna(row[sku_col]) else None),
            }
            if item["name"]:
                items.append(item)
    except Exception as e:
        raise RuntimeError(f"Не вдалося розібрати Excel/CSV: {e}")

    return {
        "supplier": None,
        "invoice_number": None,
        "invoice_date": None,
        "currency": None,
        "items": items,
        "totals": {}
    }

def _prompt_for_gpt(text: str) -> List[dict]:
    instr = (
        "Виділи з накладної структуровані дані і поверни JSON з полями: "
        "supplier, invoice_number, invoice_date (YYYY-MM-DD), currency, "
        "items (list of {name, sku, barcode, quantity, uom, price, tax, line_total}), "
        "totals {subtotal, tax, total}. Числа як числа. Якщо чогось немає — null."
    )
    return [
        {"role": "system", "content": "You are a careful invoice parser that outputs strict JSON only."},
        {"role": "user", "content": instr + "\n\n---\n" + text}
    ]

def _fallback_greedy_parse(text: str) -> Dict[str, Any]:
    # extremely simple heuristics if GPT unavailable
    supplier = None
    m = re.search(r"Supplier[:\s]+(.+)", text, re.IGNORECASE)
    if m:
        supplier = m.group(1).strip().splitlines()[0]
    inv_no = None
    for key in ["Invoice No", "Invoice #", "Накладна", "Номер"]:
        m = re.search(rf"{key}[:\s]*([A-Za-z0-9\-/]+)", text, re.IGNORECASE)
        if m:
            inv_no = m.group(1).strip()
            break
    date = None
    m = re.search(r"(\d{4}[-./]\d{1,2}[-./]\d{1,2})", text)
    if m: date = m.group(1).replace(".", "-").replace("/", "-")
    currency = None
    if " UAH" in text or "₴" in text: currency = "UAH"
    if " USD" in text or "$" in text: currency = "USD"
    # Totals
    total = None
    m = re.search(r"Total[:\s]*([0-9]+[\.,][0-9]{2})", text, re.IGNORECASE)
    if m: total = float(m.group(1).replace(",", "."))
    return {
        "supplier": supplier,
        "invoice_number": inv_no,
        "invoice_date": date,
        "currency": currency,
        "items": [],
        "totals": {"total": total} if total else {}
    }

async def parse_invoice_file(path: str, openai_key: Optional[str], default_currency: str = "UAH") -> Dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED_EXT:
        raise RuntimeError(f"Непідтримуваний формат: {ext}")

    text = ""
    if ext == ".pdf":
        text = _extract_text_from_pdf(path)
        if not text:
            text = _ocr_pdf_as_images(path)
    elif ext in {".jpg", ".jpeg", ".png", ".heic", ".heif"}:
        text = _ocr_image(path)
    elif ext in {".xls", ".xlsx", ".csv"}:
        result = _parse_excel_csv(path)
        if not result.get("currency"):
            result["currency"] = default_currency
        return result

    if not text.strip():
        raise RuntimeError("Порожній текст після OCR/екстракції")

    parsed = None

    if openai_key and OPENAI_AVAILABLE:
        try:
            client = OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=_prompt_for_gpt(text),
                temperature=0,
                response_format={"type": "json_object"}
            )
            content = resp.choices[0].message.content
            parsed = json.loads(content)
        except Exception as e:
            # fall back naive
            parsed = _fallback_greedy_parse(text)
    else:
        parsed = _fallback_greedy_parse(text)

    if not parsed:
        raise RuntimeError("Не вдалося отримати структуру накладної")

    if not parsed.get("currency"):
        parsed["currency"] = default_currency

    # Normalize items
    items = parsed.get("items") or []
    norm_items = []
    for it in items:
        norm_items.append({
            "name": (it.get("name") or "").strip(),
            "sku": (it.get("sku") or None),
            "barcode": (it.get("barcode") or None),
            "quantity": float(it.get("quantity") or 0.0),
            "uom": (it.get("uom") or None),
            "price": float(it.get("price") or 0.0),
            "tax": float(it.get("tax")) if it.get("tax") is not None else None,
            "line_total": float(it.get("line_total") or 0.0),
        })
    parsed["items"] = norm_items

    return parsed

def parse_structured_table_to_items(df_like) -> List[Dict[str, Any]]:
    # reserved for future; using _parse_excel_csv already
    return []
