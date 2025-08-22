import os
import json
import time
import asyncio
import logging
from typing import Dict, Any, List, Optional
import urllib.parse
import aiohttp

log = logging.getLogger("poster")

class PosterAPIError(Exception):
    pass

class PosterClient:
    def __init__(self, token: str):
        self.token = token
        self.base = os.getenv("POSTER_API_BASE", "https://api.joinposter.com/api").rstrip("/")
        self.m_get_products = os.getenv("POSTER_PRODUCTS_METHOD", "menu.getProducts")
        self.m_get_suppliers = os.getenv("POSTER_SUPPLIERS_METHOD", "suppliers.getSuppliers")
        self.m_create_supply = os.getenv("POSTER_CREATE_SUPPLY_METHOD", "incomingOrders.createSupply")

    async def _request(self, method: str, params: Dict[str, Any] = None, payload: Dict[str, Any] = None, retries=(3,5,8)):
        params = params or {}
        params["token"] = self.token
        url = f"{self.base}/{method}"
        # Poster API typically accepts form params; we send JSON if provided
        for attempt, delay in enumerate([0] + list(retries)):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                    if payload is None:
                        async with session.get(url, params=params) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                raise PosterAPIError(f"{resp.status}: {text}")
                            return await resp.json(content_type=None)
                    else:
                        async with session.post(url, params=params, json=payload) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                raise PosterAPIError(f"{resp.status}: {text}")
                            return await resp.json(content_type=None)
            except Exception as e:
                if attempt == len(retries):
                    raise
                log.warning(f"Poster API error ({e}), retrying in {delay}s...")
                await asyncio.sleep(delay)

    async def get_suppliers(self) -> List[Dict[str, Any]]:
        data = await self._request(self.m_get_suppliers)
        # Normalize shape: expect list of dicts with id/name
        items = []
        # Poster responses often nest data under 'response'
        arr = data.get("response") if isinstance(data, dict) else data
        if isinstance(arr, list):
            for s in arr:
                items.append({"id": s.get("supplier_id") or s.get("id"), "name": s.get("name") or s.get("supplier_name")})
        elif isinstance(arr, dict) and "suppliers" in arr:
            for s in arr["suppliers"]:
                items.append({"id": s.get("supplier_id") or s.get("id"), "name": s.get("name") or s.get("supplier_name")})
        else:
            # best-effort
            for s in (arr or []):
                items.append({"id": s.get("id"), "name": s.get("name")})
        return [x for x in items if x.get("id") and x.get("name")]

    async def get_products(self) -> List[Dict[str, Any]]:
        data = await self._request(self.m_get_products, params={"with_barcode": 1, "with_sku": 1})
        items = []
        arr = data.get("response") if isinstance(data, dict) else data
        if isinstance(arr, list):
            for p in arr:
                items.append({
                    "id": p.get("product_id") or p.get("id"),
                    "name": p.get("name") or p.get("product_name"),
                    "barcode": p.get("barcode"),
                    "sku": p.get("sku") or p.get("product_code"),
                })
        elif isinstance(arr, dict) and "products" in arr:
            for p in arr["products"]:
                items.append({
                    "id": p.get("product_id") or p.get("id"),
                    "name": p.get("name") or p.get("product_name"),
                    "barcode": p.get("barcode"),
                    "sku": p.get("sku") or p.get("product_code"),
                })
        else:
            for p in (arr or []):
                items.append({
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "barcode": p.get("barcode"),
                    "sku": p.get("sku"),
                })
        return [x for x in items if x.get("id") and x.get("name")]

    async def create_supply_from_parsed(self, parsed: Dict[str, Any], default_tax: float = 0.0) -> Any:
        # Build payload in a flexible, Poster-like shape
        supplier_id = parsed.get("supplier_id")
        supplier_name = parsed.get("supplier")
        items_payload = []
        for it in parsed.get("items", []):
            qty = float(it.get("quantity") or 0)
            price = float(it.get("price") or 0)
            tax = it.get("tax")
            items_payload.append({
                "product_id": it.get("product_id"),
                "product_name": it.get("name"),
                "quantity": qty,
                "price": price,
                "tax": tax if tax is not None else default_tax
            })
        body = {
            "supplier_id": supplier_id,
            "supplier_name": supplier_name if not supplier_id else None,
            "invoice_number": parsed.get("invoice_number"),
            "invoice_date": parsed.get("invoice_date"),
            "currency": parsed.get("currency"),
            "items": items_payload,
            "comment": "Created by Telegram import bot"
        }
        res = await self._request(self.m_create_supply, payload=body)
        # Response formats may vary. Try to extract supply ID/number
        resp = res.get("response") if isinstance(res, dict) else res
        supply_id = None
        if isinstance(resp, dict):
            supply_id = resp.get("supply_id") or resp.get("id") or resp.get("number")
        if supply_id is None and isinstance(resp, list) and resp:
            supply_id = resp[0].get("id")
        return supply_id or "unknown"
