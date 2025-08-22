import os
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple
import aiohttp
from datetime import datetime

log = logging.getLogger("poster")

class PosterAPIError(Exception):
    pass

class PosterClient:
    def __init__(self, token: str):
        self.token = token.strip()
        if not self.token:
            raise RuntimeError("POSTER_API_TOKEN is not set")

        # БАЗА з env (у тебе: https://vfm.joinposter.com/api)
        self.base = os.getenv("POSTER_API_BASE", "https://joinposter.com/api").rstrip("/")

        # Методи з env (можеш перевизначити в Railway Variables)
        self.m_get_products = os.getenv("POSTER_PRODUCTS_METHOD", "menu.getProducts")
        self.m_get_suppliers = os.getenv("POSTER_SUPPLIERS_METHOD", "suppliers.getSuppliers")

        # ГОЛОВНИЙ ФІКС: дефолтно використовуємо storage.createSupply
        self.m_create_supply = os.getenv("POSTER_CREATE_SUPPLY_METHOD", "storage.createSupply")

        # Додаткові спроби якщо 404
        self._create_fallbacks = [
            self.m_create_supply,
            "storage.createSupply",
            "incomingOrders.createIncomingOrder",
            "incomingOrders.createSupply",
        ]

        # Необов'язкові env
        self.storage_id = os.getenv("POSTER_STORAGE_ID")  # якщо знаєш ID складу – задай тут
        self._retries = (0, 3, 5, 8)

    async def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        retries: Tuple[int, ...] = None,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        params["token"] = self.token
        params.setdefault("format", "json")  # форсуємо json
        url = f"{self.base}/{method}"

        retries = retries or self._retries
        last_exc = None
        for attempt, delay in enumerate(retries):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
                    if payload is None:
                        async with session.get(url, params=params) as resp:
                            body = await resp.text()
                            if resp.status >= 400:
                                raise PosterAPIError(f"{resp.status} {url} :: {body[:400]}")
                            return await resp.json(content_type=None)
                    else:
                        async with session.post(url, params=params, json=payload) as resp:
                            body = await resp.text()
                            if resp.status >= 400:
                                raise PosterAPIError(f"{resp.status} {url} :: {body[:400]}")
                            return await resp.json(content_type=None)
            except Exception as e:
                last_exc = e
                log.warning("Poster API error (%s). URL: %s. Retry in %ss", e, url, delay)
        raise PosterAPIError(str(last_exc))

    # ---------- Довідники ----------
    async def get_suppliers(self) -> List[Dict[str, Any]]:
        data = await self._request(self.m_get_suppliers)
        arr = data.get("response") if isinstance(data, dict) else data
        items = []
        if isinstance(arr, list):
            for s in arr:
                items.append({
                    "id": s.get("supplier_id") or s.get("id"),
                    "name": s.get("name") or s.get("supplier_name"),
                })
        elif isinstance(arr, dict) and "suppliers" in arr:
            for s in arr["suppliers"]:
                items.append({
                    "id": s.get("supplier_id") or s.get("id"),
                    "name": s.get("name") or s.get("supplier_name"),
                })
        return [x for x in items if x.get("id") and x.get("name")]

    async def get_products(self) -> List[Dict[str, Any]]:
        params = {"with_barcode": 1, "with_sku": 1}
        data = await self._request(self.m_get_products, params=params)
        arr = data.get("response") if isinstance(data, dict) else data
        items = []
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
        return [x for x in items if x.get("id") and x.get("name")]

    # ---------- Створення приходу ----------
    async def create_supply_from_parsed(self, parsed: Dict[str, Any], default_tax: float = 0.0) -> Any:
        """
        Робимо payload і пробуємо кілька методів:
        - storage.createSupply  (переважно для приходу на склад)
        - incomingOrders.createIncomingOrder / incomingOrders.createSupply  (фолбеки)
        """
        # Загальні поля
        supplier_id = parsed.get("supplier_id")
        supplier_name = parsed.get("supplier")
        invoice_number = parsed.get("invoice_number")
        invoice_date = parsed.get("invoice_date") or datetime.utcnow().strftime("%Y-%m-%d")
        currency = parsed.get("currency")
        items = parsed.get("items", [])

        # 1) payload для storage.createSupply (інгредієнти/товари на склад)
        supply_block = {
            "date": f"{invoice_date} 12:00:00",
        }
        if supplier_id:
            supply_block["supplier_id"] = supplier_id
        else:
            # деякі акаунти приймають name, інші – лише id
            supply_block["supplier_name"] = supplier_name or "Unknown"

        if self.storage_id:
            supply_block["storage_id"] = int(self.storage_id)

        # елементи приходу
        ingredients = []
        for it in items:
            q = float(it.get("quantity") or 0)
            price = float(it.get("price") or 0)
            tax = it.get("tax")
            row = {
                # Poster часто приймає або id, або name
                "id": it.get("product_id"),
                "name": it.get("name"),
                "num": q,
                "price": price,
            }
            if tax is not None:
                row["tax"] = float(tax)
            ingredients.append(row)

        storage_payload = {
            "supply": supply_block,
            "ingredient": ingredients,   # інколи ключ може бути 'products' — але більшість акаунтів приймає 'ingredient'
            "invoice": {
                "number": invoice_number,
                "currency": currency
            }
        }

        # 2) універсальний payload для incomingOrders.* (якщо storage.* відсутній)
        generic_items = []
        for it in items:
            generic_items.append({
                "product_id": it.get("product_id"),
                "product_name": it.get("name"),
                "quantity": float(it.get("quantity") or 0),
                "price": float(it.get("price") or 0),
                "tax": float(it.get("tax")) if it.get("tax") is not None else default_tax
            })
        generic_payload = {
            "supplier_id": supplier_id,
            "supplier_name": supplier_name if not supplier_id else None,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "currency": currency,
            "items": generic_items,
            "comment": "Created by Telegram import bot"
        }

        # Ланцюжок спроб
        last_err = None
        for method in self._create_fallbacks:
            try:
                payload = storage_payload if method.startswith("storage.") else generic_payload
                res = await self._request(method, payload=payload)
                resp = res.get("response") if isinstance(res, dict) else res
                supply_id = None
                if isinstance(resp, dict):
                    supply_id = resp.get("supply_id") or resp.get("id") or resp.get("number")
                if supply_id is None and isinstance(resp, list) and resp:
                    supply_id = resp[0].get("id")
                log.info("Poster supply created via %s → %s", method, supply_id or "unknown")
                return supply_id or "unknown"
            except Exception as e:
                last_err = e
                log.warning("Create supply failed via %s: %s", method, e)
                # якщо це не 404 – не крутимо фолбеки безкінечно
                if " 404 " not in str(e):
                    break
        raise PosterAPIError(f"Create supply failed. Last error: {last_err}")
