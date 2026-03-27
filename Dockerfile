# Multi-stage build for linux-ai-server
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY app/ app/

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux git curl && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r aiserver && \
    useradd -r -g aiserver -d /home/aiserver -s /bin/bash aiserver && \
    mkdir -p /var/lib/linux-ai-server /var/log/linux-ai-server /var/AI-stump/agents && \
    chown -R aiserver:aiserver /var/lib/linux-ai-server /var/log/linux-ai-server /var/AI-stump

COPY --from=builder /install /usr/local
COPY app/ /app/app/
COPY config/ /app/config/

WORKDIR /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8420

USER aiserver

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8420/health'); assert r.status_code == 200"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8420"]
