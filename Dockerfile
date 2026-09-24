FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/hydroponic.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Persistent SQLite data directory (mounted as a volume in compose)
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# init_db() runs in the FastAPI lifespan startup, so launching uvicorn
# initializes the database automatically.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
