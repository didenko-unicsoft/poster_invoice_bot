import os
import time
import logging
from typing import Any, Dict, Optional, Tuple

import requests

# --- Конфіг ---
POSTER_API_TOKEN = os.environ.get("POSTER_API_TOKEN", "").strip()
if not POSTER_API_TOKEN:
    raise RuntimeError("POSTER_API_TOKEN is not set")

# Правильний базовий URL Poster API:
POSTER_API_BASE = os.getenv("POSTER_API_BASE", "https://joinposter.com/api").rstrip("/")

# Методи Poster API
METHOD_GET_SUPPLIERS = "suppliers.getSuppliers"
METHOD_GET_PRODUCTS = "menu.getProducts"
METHOD_CREATE_SUPPLY = "storage.createSupply"     # <= ключовий фікс

# Ретраї 0/3/5/8 сек за замовчанням
RETRY_DELAYS = (0, 3, 5, 8)

class PosterAPIError(Exception):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.url = url
        self.message = message


def _request(
    method: str,
    http: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
    retry_delays: Tuple[int, ...] = RETRY_DELAYS,
) -> Dict[str, Any]:
    """
    Виконує запит до Poster API:
    - token передаємо в query (?token=...)
    - GET для *get* методів, POST для створення/оновлення
    - ретраї 0/3/5/8 сек
    """
    if params is None:
        params = {}
    url = f"{POSTER_API_BASE}/{method}"
    query = {"token": POSTER_API_TOKEN, **params}

    last_err = None
    for i, delay in enumerate(retry_delays):
        try:
            if delay:
                time.sleep(delay)
            if http.upper() == "GET":
                resp = requests.get(url, params=query, timeout=30)
            else:
                # В Poster тіло краще віддавати як JSON, а token – в query
                resp = requests.post(url, params=query, json=json or {}, timeout=30)

            # Явно обробляємо 404 щоб його було видно в логах
            if resp.status_code == 404:
                raise PosterAPIError(404, f"Not found. Body: {resp.text[:300]}", url=resp.url)

            resp.raise_for_status()

            # Poster повертає JSON; інколи поле error
            data = resp.json()
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                raise PosterAPIError(int(err.get("code", 400)), str(err.get("message", "Poster API error")), url=resp.url)

            return data

        except PosterAPIError as e:
            last_err = e
            logging.warning("Poster API error (%s), retrying in %ss...", e, delay)
        except requests.RequestException as e:
            last_err = e
            logging.warning("HTTP error calling Poster (%s), retrying in %ss...", e, delay)

    # Якщо сюди дійшли — всі спроби вичерпано
    if isinstance(last_err, PosterAPIError):
        raise last_err
    raise PosterAPIError(500, f"Failed to call Poster method {method}: {last_err}", url=url)


# --- Публічні обгортки ---

def get_suppliers(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Отримати довідник постачальників"""
    return _request(METHOD_GET_SUPPLIERS, http="GET", params=params or {})


def get_products(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Отримати довідник товарів/продуктів (для матчингу за barcode/SKU)"""
    # За потреби можна додати пагінацію через 'page'/'limit'
    return _request(METHOD_GET_PRODUCTS, http="GET", params=params or {})


def create_supply(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Створити прихідну накладну в Poster.
    Очікуваний формат payload згідно Poster: {
      "supply": { "date": "YYYY-mm-dd HH:MM:SS", "supplier_id": "...", "storage_id": "...", "packing": "1" },
      "ingredient": [ { "id": "138", "type": "1", "num": "3", "sum": "6" }, ... ]
    }
    """
    return _request(METHOD_CREATE_SUPPLY, http="POST", json=payload)
