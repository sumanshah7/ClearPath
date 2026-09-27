# ClearPath PA engine — FastAPI (deploy on Railway / Render / Fly / any container host).
# Not for Vercel serverless: long-running ingest + SQLite + PDF work needs a persistent process.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data

ENV PYTHONUNBUFFERED=1
ENV DATABASE_PATH=/data/clearpath.db
ENV ARTIFACT_BUCKET=/data/artifacts

RUN mkdir -p /data/artifacts

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
