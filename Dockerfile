# Dockerfile (поклади у КОРІНЬ репозиторію, назва файлу рівно "Dockerfile")
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Системні залежності для OCR/PDF/HEIC
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-ukr \
    poppler-utils \
    libgl1 \
    libglib2.0-0 \
    libheif1 \
 && rm -rf /var/lib/apt/lists/*

# Копіюємо ВЕСЬ репозиторій (неважливо де код — у корені чи в підпапці)
WORKDIR /src
COPY . /src

# Встановлюємо залежності: шукаємо requirements.txt спочатку в корені, потім у poster_invoice_bot/
RUN set -eux; \
    if [ -f "requirements.txt" ]; then \
        pip install --no-cache-dir -r requirements.txt; \
    elif [ -f "poster_invoice_bot/requirements.txt" ]; then \
        pip install --no-cache-dir -r poster_invoice_bot/requirements.txt; \
    else \
        echo "requirements.txt not found at repo root or poster_invoice_bot/"; \
        ls -la; \
        exit 1; \
    fi

# Формуємо папку застосунку /app з правильного місця (корінь або підпапка)
RUN set -eux; \
    if [ -f "main.py" ]; then \
        mkdir -p /app && cp -r . /app; \
    elif [ -f "poster_invoice_bot/main.py" ]; then \
        mkdir -p /app && cp -r poster_invoice_bot/. /app/; \
    else \
        echo "main.py not found at repo root or poster_invoice_bot/"; \
        ls -R; \
        exit 1; \
    fi

WORKDIR /app

# Запуск бота (long-polling). У коді вже є видалення webhook на старті.
CMD ["python", "main.py"]
