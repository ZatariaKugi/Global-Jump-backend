# syntax=docker/dockerfile:1
# Multi-stage build using uv.
#
# Migrations are NOT applied during `docker build` (no database is available).
# The entrypoint / migrate script apply them at container start.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (cached layer), without the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable

# Now copy the source and install the project.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUN_MIGRATIONS=true \
    DB_WAIT_ATTEMPTS=60 \
    DB_WAIT_DELAY_SEC=1 \
    MIGRATE_ATTEMPTS=10 \
    MIGRATE_RETRY_DELAY_SEC=3

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --no-create-home appuser

WORKDIR /app
COPY --from=builder --chown=appuser:app /app /app

# Writable upload dir + executable startup scripts.
RUN mkdir -p /app/uploads \
    && chown appuser:app /app/uploads \
    && chmod +x \
        /app/scripts/docker-entrypoint.sh \
        /app/scripts/migrate.sh \
        /app/scripts/wait_for_db.sh

USER appuser
EXPOSE 8000

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
