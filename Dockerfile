FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV OMNI_DB=/tmp/omniscience.db
ENV OMNI_DB_PATH=/tmp/omniscience.db
ENV DATABASE_PATH=/tmp/omniscience.db
ENV PORT=8000

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install fastapi uvicorn websockets python-multipart || true
RUN mkdir -p /tmp /app/data && chmod -R 777 /tmp /app/data
RUN cp /app/data/omniscience.db /tmp/omniscience.db 2>/dev/null || cp omniscience.db /tmp/omniscience.db 2>/dev/null || true

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} || uvicorn app.main:app --host 0.0.0.0 --port ${PORT} || python main.py"]
