import os
import io
import json
import time
import asyncio
import logging
import hashlib
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from parser import parse_invoice_file, parse_structured_table_to_items
from matcher import (
    TTLCache, load_json, save_json, round_money, compute_sha_key,
    FuzzyMatcher, apply_unit_conversion, totals_within_tolerance
)
from poster_api import PosterClient, PosterAPIError

# ------------- Logging -------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("poster-bot")

# ------------- Env / Config -------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
POSTER_API_TOKEN = os.getenv("POSTER_API_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

FUZZY_SUPPLIER_THRESHOLD = float(os.getenv("FUZZY_SUPPLIER_THRESHOLD", "0.92"))
FUZZY_PRODUCT_THRESHOLD = float(os.getenv("FUZZY_PRODUCT_THRESHOLD", "0.90"))
ROUNDING_MODE = os.getenv("ROUNDING_MODE", "BANKERS").upper()
DEFAULT_TAX_RATE = float(os.getenv("DEFAULT_TAX_RATE", "0") or 0.0)
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "UAH")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SYN_PATH = os.path.join(DATA_DIR, "synonyms.json")
PROCESSED_PATH = os.path.join(DATA_DIR, "processed.json")

if not BOT_TOKEN or not POSTER_API_TOKEN:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN and POSTER_API_TOKEN in environment.")

synonyms = load_json(SYN_PATH, default={"suppliers": {}, "products": {}})
processed = load_json(PROCESSED_PATH, default={"keys": []})

# Cache for Poster directories
suppliers_cache = TTLCache(ttl_seconds=1800)
products_cache  = TTLCache(ttl_seconds=1800)

# ------------- Telegram Bot -------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# Poster client
poster = PosterClient(token=POSTER_API_TOKEN)

# State per chat for interactive resolution
state: Dict[int, Dict[str, Any]] = {}

# ----------- Helpers -----------
def build_supplier_keyboard(suppliers: List[Dict[str, Any]], unknown_name: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for s in suppliers[:5]:
        kb.add(InlineKeyboardButton(text=f"{s.get('name')} (#{s.get('id')})", callback_data=f"supplier:{s.get('id')}"))
    kb.add(InlineKeyboardButton(text="➕ Створити нового постачальника", callback_data="supplier:new"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel"))
    return kb

def build_product_keyboard(products: List[Dict[str, Any]], index: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for p in products[:5]:
        disp = p.get('name')
        if p.get('sku'): disp += f" · SKU {p['sku']}"
        if p.get('barcode'): disp += f" · BAR {p['barcode']}"
        kb.add(InlineKeyboardButton(text=disp, callback_data=f"product:{p.get('id')}:{index}"))
    kb.add(InlineKeyboardButton(text="➕ Створити новий товар", callback_data=f"product_new:{index}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel"))
    return kb

def build_confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(text="✅ Продовжити", callback_data="confirm:proceed"),
        InlineKeyboardButton(text="🛑 Скасувати", callback_data="confirm:cancel")
    )
    return kb

async def ensure_directories():
    for path, default in [(SYN_PATH, {"suppliers": {}, "products": {}}),
                          (PROCESSED_PATH, {"keys": []})]:
        if not os.path.exists(path):
            save_json(path, default)

# ----------- Commands -----------
@dp.message_handler(commands=["start", "help"])
async def start_cmd(message: types.Message):
    await message.reply(
        "Привіт! Надішли мені файл накладної (PDF/JPG/PNG/HEIC/XLS/XLSX/CSV) — я розпізнаю, "
        "зіставлю товари та постачальника у Poster і створю прихідну накладну.")

# ----------- Document / Photo intake -----------
@dp.message_handler(content_types=["document", "photo"])
async def handle_file(message: types.Message):
    chat_id = message.chat.id
    await ensure_directories()

    # Determine file to download
    file_id = None
    file_name = None
    if message.content_type == "document":
        file_id = message.document.file_id
        file_name = message.document.file_name or "document"
    elif message.content_type == "photo":
        file_id = message.photo[-1].file_id  # largest
        file_name = f"photo_{int(time.time())}.jpg"
    else:
        await message.reply("Надішли, будь ласка, файл з накладною.")
        return

    tg_file = await bot.get_file(file_id)
    bytes_obj = await bot.download_file(tg_file.file_path)
    content = bytes_obj.read()

    # Save temp file
    ext = os.path.splitext(file_name)[1].lower()
    if not ext:
        # Guess based on Telegram mime / fallback to .bin
        ext = ".pdf" if tg_file.file_path.lower().endswith(".pdf") else ".jpg"
    tmp_path = os.path.join(DATA_DIR, f"upload_{int(time.time())}{ext}")
    with open(tmp_path, "wb") as f:
        f.write(content)

    await message.reply("⏳ Обробляю файл (OCR/NLP + звірка з Poster)...")

    # Fetch directories (with cache)
    suppliers = suppliers_cache.get("suppliers")
    if suppliers is None:
        suppliers = await poster.get_suppliers()
        suppliers_cache.set("suppliers", suppliers)

    products = products_cache.get("products")
    if products is None:
        products = await poster.get_products()
        products_cache.set("products", products)

    # Parse
    try:
        parsed = await parse_invoice_file(tmp_path, openai_key=OPENAI_API_KEY, default_currency=DEFAULT_CURRENCY)
    except Exception as e:
        log.exception("Parse failed")
        await message.reply(f"❌ Помилка парсингу: {e}")
        return

    # Idempotency key
    sha_key = compute_sha_key(
        parsed.get('supplier') or '',
        parsed.get('invoice_number') or '',
        parsed.get('invoice_date') or '',
        parsed.get('totals', {}).get('total')
    )
    if sha_key in processed.get("keys", []):
        await message.reply("⚠️ Ця накладна вже була імпортована (дублікат за SHA-ключем). Пропускаю.")
        return

    # Match supplier
    fz = FuzzyMatcher(synonyms, FUZZY_SUPPLIER_THRESHOLD, FUZZY_PRODUCT_THRESHOLD)
    supplier_match = fz.match_supplier(parsed.get("supplier"), suppliers)

    if supplier_match is None:
        # ask user to pick supplier
        # build suggestions by fuzzy top
        sugg = fz.top_supplier_suggestions(parsed.get("supplier"), suppliers, top_n=5)
        keyboard = build_supplier_keyboard(sugg, parsed.get("supplier"))
        state[chat_id] = {"parsed": parsed, "suppliers": suppliers, "products": products,
                          "supplier_pending": parsed.get("supplier"), "unknown_items": [],
                          "sha_key": sha_key}
        await message.reply(
            f"Не знайшов постачальника «{parsed.get('supplier')}». Обери зі списку або створи нового:",
            reply_markup=keyboard)
        return
    else:
        parsed["supplier_id"] = supplier_match["id"]
        parsed["supplier"] = supplier_match["name"]

    # Match products
    unknown_items = []
    for idx, item in enumerate(parsed.get("items", [])):
        match = fz.match_product(item, products)
        if match is None:
            unknown_items.append(idx)
        else:
            item["product_id"] = match["id"]
            item["name"] = match["name"]

    # Totals check (tolerances)
    within, detail = totals_within_tolerance(parsed, rounding=ROUNDING_MODE)
    need_confirm = not within

    # If user input needed
    if unknown_items or need_confirm:
        state[chat_id] = {"parsed": parsed, "suppliers": suppliers, "products": products,
                          "supplier_pending": None, "unknown_items": unknown_items,
                          "sha_key": sha_key, "need_confirm": need_confirm}

        if unknown_items:
            idx = unknown_items[0]
            item = parsed["items"][idx]
            # suggestions
            sugg = fz.top_product_suggestions(item, products, top_n=5)
            kb = build_product_keyboard(sugg, idx)
            await message.reply(
                f"Не впізнав товар: «{item.get('name', '—')}». Обери відповідник у Poster або створити новий:",
                reply_markup=kb)
            return

        if need_confirm:
            await message.reply(
                f"⚠️ Різниця у підсумках: {detail}. Продовжити імпорт незважаючи на розбіжність?",
                reply_markup=build_confirm_keyboard())
            return

    # Create supply
    try:
        supply_id = await poster.create_supply_from_parsed(parsed, default_tax=DEFAULT_TAX_RATE)
    except PosterAPIError as e:
        await message.reply(f"❌ Не вдалося створити прихідну накладну в Poster: {e}")
        return

    # Audit + mark processed
    processed["keys"].append(sha_key)
    save_json(PROCESSED_PATH, processed)

    log_path = os.path.join(DATA_DIR, f"invoice_{int(time.time())}.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)

    await message.reply(f"✅ Імпортовано! Poster Supply ID: {supply_id}\nПостачальник: {parsed.get('supplier')} • № {parsed.get('invoice_number')} • Дата {parsed.get('invoice_date')} • Сума {parsed.get('totals',{}).get('total')} {parsed.get('currency', DEFAULT_CURRENCY)}")

# ----------- Callbacks -----------
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("supplier:"))
async def on_supplier_choice(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    st = state.get(chat_id)
    if not st:
        await callback_query.answer("Сесія не знайдена", show_alert=True)
        return

    choice = callback_query.data.split(":")[1]
    parsed = st["parsed"]

    if choice == "new":
        # Keep supplier name as-is; Poster will create it on supply creation
        parsed["supplier_id"] = None
    else:
        sel_id = int(choice)
        suppliers = st["suppliers"]
        sel = next((s for s in suppliers if int(s.get("id")) == sel_id), None)
        if not sel:
            await callback_query.answer("Не знайдено постачальника", show_alert=True)
            return
        parsed["supplier_id"] = sel["id"]
        parsed["supplier"] = sel["name"]
        # Remember alias
        orig = st.get("supplier_pending")
        if orig:
            synonyms["suppliers"][orig.lower()] = sel["id"]
            save_json(SYN_PATH, synonyms)

    # proceed to products resolution or create
    unknown = st.get("unknown_items", [])
    if unknown:
        idx = unknown[0]
        item = parsed["items"][idx]
        # suggest
        fz = FuzzyMatcher(synonyms, FUZZY_SUPPLIER_THRESHOLD, FUZZY_PRODUCT_THRESHOLD)
        sugg = fz.top_product_suggestions(item, st["products"], top_n=5)
        kb = InlineKeyboardMarkup(row_width=1)
        for p in sugg[:5]:
            disp = p.get('name')
            if p.get('sku'): disp += f" · SKU {p['sku']}"
            if p.get('barcode'): disp += f" · BAR {p['barcode']}"
            kb.add(InlineKeyboardButton(text=disp, callback_data=f"product:{p.get('id')}:{idx}"))
        kb.add(InlineKeyboardButton(text="➕ Створити новий товар", callback_data=f"product_new:{idx}"))
        kb.add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel"))
        await callback_query.message.reply(f"Не впізнав товар: «{item.get('name', '—')}». Обери відповідник:", reply_markup=kb)
    else:
        # totals?
        within, detail = totals_within_tolerance(parsed, rounding=ROUNDING_MODE)
        if not within:
            await callback_query.message.reply(f"⚠️ Різниця у підсумках: {detail}. Продовжити імпорт?", reply_markup=build_confirm_keyboard())
        else:
            try:
                supply_id = await poster.create_supply_from_parsed(parsed, default_tax=DEFAULT_TAX_RATE)
            except PosterAPIError as e:
                await callback_query.message.reply(f"❌ Помилка створення накладної: {e}")
                return
            processed["keys"].append(st["sha_key"])
            save_json(PROCESSED_PATH, processed)
            with open(os.path.join(DATA_DIR, f"invoice_{int(time.time())}.json"), "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)
            await callback_query.message.reply(f"✅ Імпортовано! Poster Supply ID: {supply_id}")
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data and (c.data.startswith("product:") or c.data.startswith("product_new:")))
async def on_product_choice(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    st = state.get(chat_id)
    if not st:
        await callback_query.answer("Сесія не знайдена", show_alert=True)
        return
    parsed = st["parsed"]
    parts = callback_query.data.split(":")
    action = parts[0]
    idx = int(parts[1] if action=="product_new" else parts[2])

    if action == "product":
        prod_id = int(parts[1])
        products = st["products"]
        sel = next((p for p in products if int(p.get("id")) == prod_id), None)
        if not sel:
            await callback_query.answer("Не знайдено товар", show_alert=True)
            return
        parsed["items"][idx]["product_id"] = sel["id"]
        # Remember alias
        alias = parsed["items"][idx].get("name")
        if alias:
            synonyms["products"][alias.lower()] = sel["id"]
            save_json(SYN_PATH, synonyms)
        parsed["items"][idx]["name"] = sel["name"]
    else:
        # product_new
        parsed["items"][idx]["product_id"] = None  # Poster will create new product by name

    # Remove from unknown queue
    st["unknown_items"] = [i for i in st["unknown_items"] if i != idx]

    # Next unresolved or totals confirm or create
    if st["unknown_items"]:
        next_idx = st["unknown_items"][0]
        item = parsed["items"][next_idx]
        fz = FuzzyMatcher(synonyms, FUZZY_SUPPLIER_THRESHOLD, FUZZY_PRODUCT_THRESHOLD)
        sugg = fz.top_product_suggestions(item, st["products"], top_n=5)
        kb = build_product_keyboard(sugg, next_idx)
        await callback_query.message.reply(f"Не впізнав товар: «{item.get('name','—')}». Обери відповідник:", reply_markup=kb)
    else:
        within, detail = totals_within_tolerance(parsed, rounding=ROUNDING_MODE)
        if not within:
            st["need_confirm"] = True
            await callback_query.message.reply(f"⚠️ Різниця у підсумках: {detail}. Продовжити імпорт?", reply_markup=build_confirm_keyboard())
        else:
            try:
                supply_id = await poster.create_supply_from_parsed(parsed, default_tax=DEFAULT_TAX_RATE)
            except PosterAPIError as e:
                await callback_query.message.reply(f"❌ Помилка створення накладної: {e}")
                return
            processed["keys"].append(st["sha_key"])
            save_json(PROCESSED_PATH, processed)
            with open(os.path.join(DATA_DIR, f"invoice_{int(time.time())}.json"), "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)
            await callback_query.message.reply(f"✅ Імпортовано! Poster Supply ID: {supply_id}")

    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("confirm:"))
async def on_confirm(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    st = state.get(chat_id)
    if not st:
        await callback_query.answer("Сесія не знайдена", show_alert=True)
        return
    parsed = st["parsed"]
    if callback_query.data.endswith("proceed"):
        try:
            supply_id = await poster.create_supply_from_parsed(parsed, default_tax=DEFAULT_TAX_RATE)
        except PosterAPIError as e:
            await callback_query.message.reply(f"❌ Помилка створення накладної: {e}")
            return
        processed["keys"].append(st["sha_key"])
        save_json(PROCESSED_PATH, processed)
        with open(os.path.join(DATA_DIR, f"invoice_{int(time.time())}.json"), "w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False, indent=2)
        await callback_query.message.reply(f"✅ Імпортовано! Poster Supply ID: {supply_id}")
    else:
        await callback_query.message.reply("Операцію скасовано.")
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "cancel")
async def on_cancel(callback_query: types.CallbackQuery):
    await callback_query.message.reply("Операцію скасовано.")
    await callback_query.answer()


async def on_startup(dp):
    # Ensure long polling works if webhook was previously set
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook warning: {e}")

# ----------- Main -----------
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
