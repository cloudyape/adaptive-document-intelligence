FROM python:3.12-slim
# Tesseract enables the local OCR path (cheaper MEDIUM-tier extraction, measured OCR confidence for routing)
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV ADI_STORAGE_DIR=/data/storage
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
