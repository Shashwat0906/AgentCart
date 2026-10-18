# Infrastructure files

Infrastructure files live where their tools expect them. This index lists them all:

| File | Purpose |
|---|---|
| [`../docker-compose.yml`](../docker-compose.yml) | Local stack: PostgreSQL 16, backend, nginx frontend, and an optional `ollama` profile |
| [`../backend/Dockerfile`](../backend/Dockerfile) | Multi-stage Python 3.14 image, non-root, health check |
| [`../backend/docker-entrypoint.sh`](../backend/docker-entrypoint.sh) | `alembic upgrade head` → optional seed → uvicorn |
| [`../frontend/Dockerfile`](../frontend/Dockerfile) | Node build → unprivileged nginx |
| [`../frontend/nginx.conf.template`](../frontend/nginx.conf.template) | SPA routing, `/api` reverse proxy, CSP and security headers |
| [`../render.yaml`](../render.yaml) | Render Blueprint for the API (free plan) |
| [`../frontend/vercel.json`](../frontend/vercel.json) | Vercel SPA rewrites, caching and security headers |
| [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) | CI: lint, types, tests on Postgres, evaluation, audits, E2E, Docker build, secret scan |
| [`../scripts/setup-env.sh`](../scripts/setup-env.sh) | Generates `.env` with random local secrets |

See [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md) for hosting steps and free-tier caveats.
