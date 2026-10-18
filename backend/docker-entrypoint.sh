#!/bin/sh
# Apply migrations, optionally seed, then start the API.
set -eu

echo "Applying database migrations…"
alembic upgrade head

if [ "${AUTO_SEED:-true}" = "true" ]; then
  python -m app.seed
fi

# FORWARDED_ALLOW_IPS: which proxies may set X-Forwarded-For (used for per-IP rate limits).
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --workers "${WEB_CONCURRENCY:-1}"
