import os
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple
import aiohttp
from datetime import datetime

log = logging.getLogger("poster")


class PosterAPIError(Exception):
    pass


class PosterClient:
    """
    Async клієнт Poster API з фолбеками під різні акаунти.
    Працює з базою з ENV: POSTER_API_BASE = https://<account>.joinposter.com/api
    """
    def __init__(self, token: str):
        self.token = (token or "").strip()
        if not self.token:
            raise RuntimeError("POSTER_API_TOKEN is not set")

        # У тебе це: https://vfm.joinposter.com/api
        self.base = os.getenv("POSTER_API_BASE", "https://joinposter.com/api").rstrip("/")

        # Продукти: за замовчуванням menu.getProducts
        self.m_get_products = os.getenv("POSTER_PRODUCTS_METHOD", "menu.getProducts")

        # ======== SUPPLIERS: порядок спроб (спочатку clients.getSuppliers!) ========
        env_sup = os.getenv("POSTER_SUPPLIERS_METHOD")
        self._suppliers_methods: List[str] = []

        # Завжди починаємо з clients.getSuppliers — він зазвичай працює на PRO-доменах
        self._suppliers_methods.append("clients.getSuppliers")

        # Якщо користувач явно задав метод у Variables — спробуємо його другим
        if env_sup and env_sup not in self._suppliers_methods:
            self._suppliers_methods.append(env_sup)

        # Інші поширені варіанти
        for cand in ("suppliers.getSuppliers", "clients.getContractors"):
            if cand not in self._suppliers_methods:
                self._suppliers_methods.append(cand)

        # ======== CREATE SUPPLY: головний + фолбеки ========
        env_create = os.getenv("POSTER_CREATE_SUPPLY_METHOD")
        self._create_methods: List[str] = []
        # основний
        self._create_methods.append(env_create or "storage.createSupply")
        # фолбеки
        for cand in ("incomingOrders.createIncomingOrder", "incomingOrders.createSupply"):
            if cand not in self._create_methods:
                self._create_methods.append(cand)

        # (опц.) ID складу (spot/storage), якщо кілька локацій
        self.storage_id = os.getenv("POSTER_STORAGE_ID")

        # Ретраї 0/3/5/8 сек
        self._retries = (0, 3, 5, 8)

    # -------------------- HTTP --------------------

    async def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        retries: Optional[Tuple[int, ...]] = None,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        params["token"] = self.token
        params.setdefault("format", "json")
        url = f"{self.base}/{method}"

        retries = retries or self._retries
        last_exc: Optional[Exception] = None

        for delay in retries:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
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
                # Не шумимо зайвим, просто інформативний лог і рухаємось далі
                log.warning("Poster API error (%s). URL: %s. Retry in %ss", e, url, delay)

        raise PosterAPIError(str(last_exc) if last_exc else f"Request failed: {url}")

    # -------------------- Довідники --------------------

    async def get_suppliers(self) -> List[Dict[str, Any]]:
        """
        Повертає [{"id": ..., "name": ...}, ...]
        Послідовно пробуємо кілька методів; першим — clients.getSuppliers.
        """
        last_err = None
        for method in self._suppliers_methods:
            try:
                data = await self._request(method)
                arr = data.get("response") if isinstance(data, dict) else data
                items: List[Dict[str, Any]] = []

                if isinstance(arr, list):
                    for s in arr:
                        items.append({
                            "id": s.get("supplier_id") or s.get("id"),
                            "name": s.get("name") or s.get("supplier_name"),
                        })
                elif isinstance(arr, dict):
                    # можливі ключі: "suppliers", "contractors"
                    src = arr.get("suppliers") or arr.get("contractors") or []
                    for s in src:
                        items.append({
                            "id": s.get("supplier_id") or s.get("id"),
                            "name": s.get("name") or s.get("supplier_name"),
                        })

                items = [x for x in items if x.get("id") and x.get("name")]
                if items:
                    log.info("Suppliers fetched via %s: %d", method, len(items))
                    return items

                last_err = PosterAPIError(f"Empty suppliers response via {method}")
                log.warning("get_suppliers via %s: empty response", method)

            except Exception as e:
                last_err = e
                # Якщо 404 — просто ідемо далі на наступний метод, без паніки
                if " 404 " in str(e):
                    log.info("get_suppliers via %s failed with 404 (URL в логах). Пробую наступний метод.", method)
                else:
                    log.warning("get_suppliers via %s failed: %s", method, e)

        # Якщо все впало/порожньо — повертаємо пустий список (бот запитає мапінг у користувача)
        log.error("All supplier methods failed, last error: %s", last_err)
        return []

    async def get_products(self) -> List[Dict[str, Any]]:
        """
        Отримує продукти/товари для матчингу (за замовчуванням menu.getProducts).
        """
        params = {"with_barcode": 1, "with_sku": 1}
        data = await self._request(self.m_get_products, params=params)
        arr = data.get("response") if isinstance(data, dict) else data
        items: List[Dict[str, Any]] = []

        if isinstance(arr, list):
            for p in arr:
                items.append({
                    "id": p.get("product_id") or p.get("id"),
                    "name": p.get("name") or p.get("product_name"),
                    "barcode": p.get("barcode"),
                    "sku": p.get("sku") or p.get("product_code"),
                })
        elif isinstance(arr, dict):
            src = arr.get("products") or arr.get("menu") or []
            for p in src:
                items.append({
                    "id": p.get("product_id") or p.get("id"),
                    "name": p.get("name") or p.get("product_name"),
                    "barcode": p.get("barcode"),
                    "sku": p.get("sku") or p.get("product_code"),
                })

        items = [x for x in items if x.get("id") and x.get("name")]
        log.info("Products fetched via %s: %d", self.m_get_products, len(items))
        return items

    # -------------------- Прихід (накладна) --------------------

    async def create_supply_from_parsed(self, parsed: Dict[str, Any], default_tax: float = 0.0) -> Any:
        """
        Створює прихід у Poster. Послідовно пробує методи зі списку _create_methods.
        Повертає supply_id/number або 'unknown'.
        """
        supplier_id = parsed.get("supplier_id")
        supplier_name = parsed.get("supplier")
        invoice_number = parsed.get("invoice_number")
        invoice_date = parsed.get("invoice_date") or datetime.utcnow().strftime("%Y-%m-%d")
        currency = parsed.get("currency")
        items = parsed.get("items", [])

        # payload для storage.createSupply
        supply_block: Dict[str, Any] = {
            "date": f"{invoice_date} 12:00:00",
        }
        if supplier_id:
            supply_block["supplier_id"] = supplier_id
        elif supplier_name:
            # деякі акаунти приймають name
            supply_block["supplier_name"] = supplier_name

        if self.storage_id:
            try:
                supply_block["storage_id"] = int(self.storage_id)
            except Exception:
                supply_block["storage_id"] = self.storage_id

        ingredients: List[Dict[str, Any]] = []
        for it in items:
            q = float(it.get("quantity") or 0)
            price = float(it.get("price") or 0)
            row: Dict[str, Any] = {
                "id": it.get("product_id"),
                "name": it.get("name"),
                "num": q,
                "price": price,
            }
            if it.get("tax") is not None:
                row["tax"] = float(it["tax"])
            ingredients.append(row)

        storage_payload = {
            "supply": supply_block,
            "ingredient": ingredients,  # в окремих акаунтах ключ може бути 'products'
            "invoice": {
                "number": invoice_number,
                "currency": currency
            }
        }

        # payload для incomingOrders.* (фолбек)
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
            "comment": "Created by Telegram import bot",
        }

        last_err = None
        for method in self._create_methods:
            if not method:
                continue
            try:
                payload = storage_payload if method.startswith("storage.") else generic_payload
                res = await self._request(method, payload=payload)
                resp = res.get("response") if isinstance(res, dict) else res
                supply_id = None
                if isinstance(resp, dict):
                    supply_id = resp.get("supply_id") or resp.get("id") or resp.get("number")
                if supply_id is None and isinstance(resp, list) and resp:
                    supply_id = resp[0].get("id")
                log.info("Supply created via %s → %s", method, supply_id or "unknown")
                return supply_id or "unknown"
            except Exception as e:
                last_err = e
                log.warning("Create supply failed via %s: %s", method, e)
                # якщо це не 404 — далі немає сенсу ганяти всі фолбеки
                if " 404 " not in str(e):
                    break

        raise PosterAPIError(f"Create supply failed. Last error: {last_err}")
