# syntax=docker/dockerfile:1

# E-Ticket Generator — Flask app served by Gunicorn.
# Deploy target: Coolify, Dockerfile build pack.
# Coolify injects PORT (default "Ports Exposes" 3000); the app binds to it.
# Requires SECRET_KEY and ADMIN_PASSWORD in Coolify's environment variables;
# PRODUCTION=1 makes the app fail closed when they are missing.

FROM python:3.11.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=3000 \
    PRODUCTION=1 \
    DATA_FILE=/data/promo_records.json \
    CONTACTS_FILE=/data/telegram_contacts.json

WORKDIR /app

# curl is only for the container healthcheck; Coolify expects curl or wget.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies are installed in their own layer so code edits rebuild fast.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Runtime code and assets. No .env, records, tests, or venv (see .dockerignore).
COPY app.py ticket_core.py record_store.py telegram_bot.py ./
COPY fonts ./fonts
COPY templates ./templates
COPY static ./static
COPY ticket_template.jpg ./

# Run unprivileged. /data is the mount point for Coolify persistent storage;
# creating it here lets a named volume inherit the right ownership.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app

EXPOSE 3000

# GET / is the app's liveness endpoint. Shell form so ${PORT} expands.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-3000}/" -o /dev/null || exit 1

# One worker by default: JSON storage uses in-process locks, so concurrent
# workers would race on DATA_FILE. Raise WEB_CONCURRENCY only with Upstash.
# --timeout 120 leaves room for the app's ~50s Telegram delivery budget.
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT:-3000} --workers ${WEB_CONCURRENCY:-1} --threads ${GUNICORN_THREADS:-8} --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile -"]
