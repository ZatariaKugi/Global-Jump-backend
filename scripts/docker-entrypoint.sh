#!/bin/sh
# Container entrypoint: apply Alembic migrations, then start the app.
# Migrations cannot run at image *build* time (no database). They run on
# every container start / deploy — same pattern as Rails, Django, etc.
set -eu

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Running database migrations (alembic upgrade head)..."
  alembic upgrade head
  echo "Migrations complete."
else
  echo "Skipping migrations (RUN_MIGRATIONS=${RUN_MIGRATIONS})."
fi

exec "$@"
