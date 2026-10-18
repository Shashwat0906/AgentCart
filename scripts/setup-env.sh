#!/usr/bin/env bash
# Creates .env from .env.example with freshly generated local secrets.
# Never overwrites an existing .env. Secrets are generated locally and never committed.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists — leaving it untouched."
  exit 0
fi

gen() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
JWT_SECRET="$(gen 48)"
PG_PASSWORD="$(gen 24 | tr -d '_-')"

sed -e "s|^JWT_SECRET=.*|JWT_SECRET=${JWT_SECRET}|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PASSWORD}|" \
    -e "s|change-me-local-only|${PG_PASSWORD}|g" \
    .env.example > .env
chmod 600 .env
echo "Created .env with a random JWT_SECRET and Postgres password (file mode 600)."
