FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY LICENSE .
COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /data \
    && chmod +x ./scripts/validate.sh ./scripts/modbus_smoke.py \
    && chown -R app:app /app /data

ENV HTTP_PORT=80 \
    STATE_PATH=/data/state.json \
    PYTHONUNBUFFERED=1

EXPOSE 80 502

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${HTTP_PORT}/healthz" || exit 1

USER app

CMD ["python", "-m", "app.wire_server"]
