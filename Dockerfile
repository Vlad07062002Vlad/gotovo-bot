# --- минимальный образ, без Paketo/GCR ---
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/4.00/tessdata \
    TESS_LANGS=bel+rus+eng \
    TESS_CONFIG="--oem 3 --psm 6 -c preserve_interword_spaces=1"

WORKDIR /app

# Системные deps + Tesseract с языками RU/BE и OSD (для авто-поворота)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-bel tesseract-ocr-osd \
    libglib2.0-0 libsm6 libxext6 libxrender1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Кладём код
COPY . .

EXPOSE 8080
# Стартуем бота
CMD ["python", "bot.py"]


