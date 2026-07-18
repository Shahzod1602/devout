# syntax=docker/dockerfile:1.7
# Multi-stage build — botprod/Dockerfile bilan bir xil shablon.

# ============================================================
# STAGE 1: Builder
# ============================================================
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ============================================================
# STAGE 2: Runtime
# ============================================================
FROM python:3.11-slim AS runtime

# libgl/libglib paperwork PDF/image ishlov uchun (PyMuPDF, PIL deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd -r bot && useradd -r -g bot bot

WORKDIR /app

COPY --from=builder /install /usr/local

# Application — root-level Python modullarini bir glob bilan
COPY --chown=bot:bot *.py ./
COPY --chown=bot:bot stats/ ./stats/
COPY --chown=bot:bot external/ ./external/
COPY --chown=bot:bot paperwork/ ./paperwork/
COPY --chown=bot:bot db/ ./db/
COPY --chown=bot:bot storage/ ./storage/
COPY --chown=bot:bot api/ ./api/
COPY --chown=bot:bot telegram/ ./telegram/
COPY --chown=bot:bot static/ ./static/

# Persistent data dir (docker volume bot_data shu yerga mount qilinadi)
RUN mkdir -p /app/data && chown bot:bot /app/data

USER bot

ARG BOT_PORT=8045
ENV BOT_PORT=${BOT_PORT}
EXPOSE ${BOT_PORT}

ENV LOG_FORMAT=text \
    LOG_LEVEL=INFO

CMD ["python", "-u", "bot.py"]
