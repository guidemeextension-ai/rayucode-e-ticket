# syntax=docker/dockerfile:1

# E-Ticket Generator — Flask app served by Gunicorn.
# Deploy target: Coolify, Dockerfile build pack.
# In Coolify set "Ports Exposes" to 8000 (or change PORT/EXPOSE below to match).
# Requires SECRET_KEY and ADMIN_PASSWORD in Coolify's environment variables;
# PRODUCTION=1 makes the app fail closed when they are missing.

FROM python:3.11.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    PRODUCTION=1 \
    DATA_FILE=/data/promo_records.json \
    CONTACTS_FILE=/data/telegram_contacts.json

WORKDIR /app

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

EXPOSE 8000

# GET / is the app's liveness endpoint. Uses only stdlib (no curl in slim).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/', timeout=3)"

# One worker by default: JSON storage uses in-process locks, so concurrent
# workers would race on DATA_FILE. Raise WEB_CONCURRENCY only with Upstash.
# --timeout 120 leaves room for the app's ~50s Telegram delivery budget.
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --threads ${GUNICORN_THREADS:-8} --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile -"]
