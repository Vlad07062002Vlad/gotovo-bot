FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data \
    METRICS_PATH=/data/metrics.json \
    METRICS_AUTOSAVE_SEC=60

WORKDIR /app

# Tesseract + языковые пакеты (рус/бел/нем/фр + OSD)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-bel \
        tesseract-ocr-deu \
        tesseract-ocr-fra \
        tesseract-ocr-osd && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-u", "bot.py"]

