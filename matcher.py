import os
import json
import math
import time
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from rapidfuzz import fuzz, process

# --------- Persistence helpers ---------
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

class TTLCache:
    def __init__(self, ttl_seconds: int = 1200):
        self.ttl = ttl_seconds
        self.store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str):
        val = self.store.get(key)
        if not val: return None
        ts, data = val
        if time.time() - ts > self.ttl:
            self.store.pop(key, None)
            return None
        return data

    def set(self, key: str, value):
        self.store[key] = (time.time(), value)

# --------- Money & rounding ---------
def bankers_round(value: float, ndigits: int = 2) -> float:
    # round-half-to-even
    q = 10 ** ndigits
    return float(f"{round(value * q) / q:.{ndigits}f}")

def round_money(value: float, mode: str = "BANKERS", ndigits: int = 2) -> float:
    if mode.upper() == "BANKERS":
        return bankers_round(value, ndigits)
    return round(value, ndigits)

# --------- Idempotency ---------
def compute_sha_key(supplier: str, number: str, date: str, total: Any) -> str:
    raw = f"{supplier or ''}|{number or ''}|{date or ''}|{total or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# --------- Fuzzy Matcher ---------
class FuzzyMatcher:
    def __init__(self, synonyms: Dict[str, Dict[str, Any]], supplier_thr: float, product_thr: float):
        self.syn = synonyms
        self.s_thr = supplier_thr
        self.p_thr = product_thr

    # Supplier matching
    def match_supplier(self, name: Optional[str], suppliers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        # synonyms first
        sid = self.syn.get("suppliers", {}).get(name.lower())
        if sid:
            found = next((s for s in suppliers if str(s.get("id")) == str(sid)), None)
            if found: return {"id": found["id"], "name": found["name"]}
        # exact case-insensitive
        exact = next((s for s in suppliers if s.get("name", "").lower() == name.lower()), None)
        if exact:
            return {"id": exact["id"], "name": exact["name"]}
        # fuzzy
        names = {s.get("name",""): s for s in suppliers}
        choice, score, _ = process.extractOne(name, names.keys(), scorer=fuzz.WRatio) if names else (None, 0, None)
        if choice and (score/100.0) >= self.s_thr:
            s = names[choice]
            return {"id": s.get("id"), "name": s.get("name")}
        return None

    def top_supplier_suggestions(self, name: str, suppliers: List[Dict[str, Any]], top_n=5) -> List[Dict[str, Any]]:
        names = {s.get("name",""): s for s in suppliers}
        results = process.extract(name, names.keys(), scorer=fuzz.WRatio, limit=top_n) if names else []
        return [names[r[0]] for r in results]

    # Product matching
    def match_product(self, item: Dict[str, Any], products: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        # codes
        if item.get("barcode"):
            p = next((x for x in products if str(x.get("barcode") or "") == str(item["barcode"])), None)
            if p: return {"id": p["id"], "name": p["name"]}
        if item.get("sku"):
            p = next((x for x in products if str(x.get("sku") or "") == str(item["sku"])), None)
            if p: return {"id": p["id"], "name": p["name"]}
        # synonyms
        name = (item.get("name") or "").lower()
        pid = self.syn.get("products", {}).get(name)
        if pid:
            p = next((x for x in products if str(x.get("id")) == str(pid)), None)
            if p: return {"id": p["id"], "name": p["name"]}
        # exact
        p = next((x for x in products if (x.get("name","")).lower() == name), None)
        if p: return {"id": p["id"], "name": p["name"]}
        # fuzzy
        names = {x.get("name",""): x for x in products}
        choice, score, _ = process.extractOne(name, names.keys(), scorer=fuzz.WRatio) if names else (None, 0, None)
        if choice and (score/100.0) >= self.p_thr:
            p = names[choice]
            return {"id": p.get("id"), "name": p.get("name")}
        return None

    def top_product_suggestions(self, item: Dict[str, Any], products: List[Dict[str, Any]], top_n=5) -> List[Dict[str, Any]]:
        name = item.get("name") or ""
        names = {x.get("name",""): x for x in products}
        results = process.extract(name, names.keys(), scorer=fuzz.WRatio, limit=top_n) if names else []
        return [names[r[0]] for r in results]

# --------- Unit conversions & totals ---------
def apply_unit_conversion(item: Dict[str, Any], conv: Dict[str, float]) -> Dict[str, Any]:
    q = item.get("quantity", 0.0)
    uom = (item.get("uom") or "").lower()
    if uom and uom in conv:
        factor = conv[uom]
        item["quantity"] = q * factor
        item["uom"] = None  # normalized to Poster unit
    return item

def totals_within_tolerance(parsed: Dict[str, Any], rounding: str = "BANKERS", tol_pct=0.005, tol_abs=0.50) -> (bool, str):
    items = parsed.get("items", [])
    subtotal = 0.0
    tax_total = 0.0
    for it in items:
        line = (it.get("price") or 0.0) * (it.get("quantity") or 0.0)
        subtotal += line
        if it.get("tax") is not None:
            tax_total += line * (float(it["tax"]) / 100.0)
    subtotal = round_money(subtotal, rounding)
    tax_total = round_money(tax_total, rounding)
    comp_total = round_money(subtotal + tax_total, rounding)

    declared = parsed.get("totals", {}).get("total")
    if declared is None:
        return True, "В інвойсі відсутній підсумок, пропускаю звірку."
    declared = float(declared)
    diff = abs(declared - comp_total)
    within = (diff <= tol_abs) or (diff <= tol_pct * max(1.0, declared))
    detail = f"Обчислено {comp_total} vs заявлено {declared} (різниця {diff})"
    return within, detail
