# Support-automation runtime — one image, three roles.
#
# The same image runs the mailbox poller, the job worker, and the API
# (Salesforce push webhook); docker-compose.yml picks the role per service
# with `command:`. Built for a local always-on box (WSL2 / Docker Desktop) —
# no cloud account, no credit card.
FROM python:3.12-slim

# curl: compose healthcheck for the api service. ca-certificates: TLS to
# Supabase / Salesforce / Groq / Gmail.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# deps first so a code change doesn't reinstall the world
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# stdout unbuffered so `docker compose logs -f` is live; the fastembed
# ONNX model downloads once into ~/.cache (a named volume in compose).
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface \
    FASTEMBED_CACHE_PATH=/root/.cache/fastembed

# default role; every compose service overrides this
CMD ["python", "-m", "api.worker"]
