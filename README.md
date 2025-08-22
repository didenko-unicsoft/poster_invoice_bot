# Poster Invoice Import Telegram Bot

A production-ready Telegram bot that imports supplier invoices into **Poster POS**.

**Features**
- Accepts PDF, JPG, PNG, HEIC, XLS/XLSX, CSV via Telegram
- OCR (Tesseract) + PDF text extraction + optional GPT parsing to structured JSON
- Poster POS integration: fetch suppliers/products (cached), create supply (incoming) document
- Matching pipeline: barcode → sku → saved synonyms → fuzzy (suppliers ≥0.92, products ≥0.90)
- Unit conversions via config, total verification with tolerance (±0.5% or ±0.50 UAH)
- Idempotency via SHA256(supplier+number+date+total)
- Human-in-the-loop: inline buttons for unknown supplier/product and totals mismatch
- Retries (3/5/8s), SLO target ≤ 60s per document
- Audit logs and persisted mappings in ./data

## Quick Start (Local)

1. **Install system deps** (Debian/Ubuntu example):
   ```bash
   sudo apt-get update
   sudo apt-get install -y tesseract-ocr poppler-utils
   ```

2. **Python 3.11**, then install:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure .env** (copy and edit):
   ```bash
   cp .env.example .env
   ```

4. **Run bot**:
   ```bash
   python main.py
   ```

## Deploy on Railway

- Push this repo to GitHub and connect it in Railway.
- Railway will use the provided **Dockerfile**; add environment variables in the project settings.
- No public web port is needed (bot runs as a worker).

## Environment Variables

- `TELEGRAM_BOT_TOKEN` *(required)* — Telegram bot token
- `POSTER_API_TOKEN` *(required)* — Poster API token
- `OPENAI_API_KEY` *(optional but recommended)* — for GPT parsing
- `FUZZY_SUPPLIER_THRESHOLD` (default `0.92`)
- `FUZZY_PRODUCT_THRESHOLD` (default `0.90`)
- `ROUNDING_MODE` (default `BANKERS`)
- `DEFAULT_TAX_RATE` (e.g. `20` for 20%)
- `DEFAULT_CURRENCY` (default `UAH`)
- **Poster endpoints (override if needed):**
  - `POSTER_API_BASE` (default `https://api.joinposter.com/api`)
  - `POSTER_PRODUCTS_METHOD` (default `menu.getProducts`)
  - `POSTER_SUPPLIERS_METHOD` (default `suppliers.getSuppliers`)
  - `POSTER_CREATE_SUPPLY_METHOD` (default `incomingOrders.createSupply`)

> ⚠️ Poster API endpoints may vary by account/version. If you get 404/400 from Poster, change the `*_METHOD` envs to match your account’s documented methods.

## Files & Folders

- `main.py` — Telegram bot & workflow
- `parser.py` — OCR + PDF + Excel/CSV parsing and GPT extraction
- `poster_api.py` — Poster client (cached fetch, create supply, retries)
- `matcher.py` — fuzzy matching, synonyms, idempotency (SHA256), unit conversions
- `requirements.txt` — Python deps
- `Dockerfile` — Railway container build
- `Procfile` — worker process entry
- `data/` — `synonyms.json`, `processed.json`, logs (`*.json` per invoice)

## Notes

- HEIC images require Pillow-HEIF (installed) but OS codecs may be needed on some hosts.
- Tesseract language packs: install additional languages if your invoices are not in English/UA.
- The GPT call uses JSON output mode; if your model doesn’t support it, the code falls back to text parsing.
- The Poster client ships with safe defaults and an override mechanism — see environment variables.
