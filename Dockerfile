FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENV_FILE=/app/.env \
    DB_PATH=/app/data/spam_filter.db \
    LOG_DIR=/app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY imap_bridge.py dashboard.py check_folders.py ./
COPY templates/ ./templates/

RUN mkdir -p /app/data

EXPOSE 8025

CMD ["python3", "imap_bridge.py"]
