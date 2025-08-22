# Dockerfile (клади в КОРІНЬ репозиторію)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# System deps for OCR/PDF/HEIC
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-ukr \
    poppler-utils \
    libgl1 \
    libglib2.0-0 \
    libheif1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) спочатку залежності (для кешу)
COPY poster_invoice_bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 2) копіюємо сам застосунок з підпапки
COPY poster_invoice_bot/ /app/

# 3) стартуємо бота
CMD ["python", "main.py"]
