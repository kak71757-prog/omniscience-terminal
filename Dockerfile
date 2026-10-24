# =============================================================================
# PROJECT OMNISCIENCE — Railway / container image
# Python 3.11 slim. Single uvicorn process: the engine holds websocket streams,
# the trained models and the broker state in memory, so it must NOT be forked
# across workers.
# =============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    OMNI_DB=/app/data/omniscience.db

WORKDIR /app

# curl is used by the healthcheck; the rest of the runtime needs no OS packages
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# dependency layer — cached unless requirements.txt changes
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# application
COPY run.sh ./
COPY server ./server
COPY web ./web
COPY tests ./tests

# persistent state: SQLite trades/journal + trained model weights.
# Mount a Railway volume here or state resets on every deploy.
RUN mkdir -p /app/data/models
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# railway start command: bash run.sh
CMD ["bash", "run.sh"]
