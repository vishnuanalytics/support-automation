# All-in-one image for single-container free hosts (Hugging Face Spaces,
# Render, Koyeb). Runs worker + cdc + poller under deploy/run_all.py with a
# health port. Multi-service platforms (Railway) should use the ROOT
# Dockerfile + Procfile instead — one process per service.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HF Spaces runs the container as uid 1000; keep model/HF caches in /tmp
# (always writable, ephemeral — fastembed re-fetches its ~85 MB model on a
# cold start, which is fine).
RUN useradd -m -u 1000 app || true
USER app

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/tmp/hf \
    HF_HUB_CACHE=/tmp/hf \
    FASTEMBED_CACHE_PATH=/tmp/fastembed \
    PORT=7860

EXPOSE 7860
CMD ["python", "deploy/run_all.py"]
