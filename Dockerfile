# Dockerfile у КОРЕНІ репозиторію
FROM python:3.11-bookworm

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

# Копіюємо весь репозиторій
WORKDIR /src
COPY . /src

# Встановлюємо Python-залежності
# (requirements.txt вже лежить у корені; якщо у вас він в підпапці, змініть шлях)
RUN pip install --no-cache-dir -r requirements.txt

# Готуємо робочу папку застосунку
RUN mkdir -p /app && cp -r /src/. /app/
WORKDIR /app

# Старт бота (long polling)
CMD ["python", "main.py"]
