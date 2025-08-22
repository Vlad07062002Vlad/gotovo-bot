# --- минимальный образ, без Paketo/GCR ---
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    # По умолчанию приоритет русского + бел/англ/нем/фр
    TESS_LANGS="rus+bel+eng+deu+fra" \
    TESS_CONFIG="--oem 3 --psm 6 -c preserve_interword_spaces=1"

WORKDIR /app

# Ставим Tesseract + языки + osd
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-osd \
      tesseract-ocr-rus \
      tesseract-ocr-bel \
      tesseract-ocr-deu \
      tesseract-ocr-fra \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код
COPY . .

# Стартуем бота
CMD ["python", "bot.py"]
