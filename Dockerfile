# Support-automation runtime — one image, three roles.
#
# The same image runs the mailbox poller, the job worker, and the API
# (Salesforce push webhook); docker-compose.yml picks the role per service
# with `command:`. Built for a local always-on box (WSL2 / Docker Desktop) —
# no cloud account, no credit card.
FROM python:3.12-slim

# MEDIA=1 pulls in the Phase 25 attachment OCR / video stack (RapidOCR,
# faster-whisper, OpenCV libs, ffmpeg). Off by default — keeps the image
# light and the build fast (matters on the Oracle free VM). The code is
# import-guarded, so MEDIA=0 just means "no text-in-image / no video".
ARG MEDIA=0

# curl: compose healthcheck for the api service. ca-certificates: TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && if [ "$MEDIA" = "1" ]; then \
         apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libxcb1 ffmpeg ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# deps first so a code change doesn't reinstall the world
COPY requirements.txt requirements-media.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$MEDIA" = "1" ]; then pip install --no-cache-dir -r requirements-media.txt ; fi

COPY . .

# stdout unbuffered so `docker compose logs -f` is live; the fastembed
# ONNX model downloads once into ~/.cache (a named volume in compose).
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface \
    FASTEMBED_CACHE_PATH=/root/.cache/fastembed

# default role; every compose service overrides this
CMD ["python", "-m", "api.worker"]
