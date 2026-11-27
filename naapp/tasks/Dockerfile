# syntax=docker/dockerfile:1
# Build context: the repository root, because tasks depends on the in-repo
# naapp-lib:  docker build -f naapp/tasks/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8765 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv binary from the official image — no pip anywhere in the build.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

# /app mirrors the repository layout, so the path source ../../naapp-lib
# resolves in the image as it does in a checkout. Only the paths the service
# needs are copied, so nothing local to a checkout (.venv, .env) enters the image.
COPY naapp-lib/pyproject.toml naapp-lib/README.md /app/naapp-lib/
COPY naapp-lib/src /app/naapp-lib/src

WORKDIR /app/naapp/tasks

# Resolve dependencies from the committed lock first (cached layer), then the
# project, with `uv sync --frozen` so the build is reproducible and never re-resolves.
COPY naapp/tasks/pyproject.toml naapp/tasks/uv.lock naapp/tasks/README.md ./
RUN uv sync --frozen --no-install-project --no-dev
COPY naapp/tasks/src ./src
RUN uv sync --frozen --no-dev

RUN groupadd --gid 10001 tasks \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin tasks
USER 10001:10001

EXPOSE 8765
# Probes /live, which checks no dependency: a database outage must not make an
# orchestrator restart a process that is serving requests.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD ["/app/.venv/bin/python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/live',timeout=4).close()"]
CMD ["sh", "-c", "/app/.venv/bin/uvicorn aweb_tasks.app:serve --factory --host 0.0.0.0 --port ${PORT:-8765}"]
