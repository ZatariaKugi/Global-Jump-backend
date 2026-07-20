#!/bin/sh
# Container entrypoint: optionally migrate, then start the app command.
# Migrations are not run at image build time (no database available then).
set -eu

echo "Waiting for database..."
/app/scripts/wait_for_db.sh

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  attempts="${MIGRATE_ATTEMPTS:-10}"
  delay="${MIGRATE_RETRY_DELAY_SEC:-3}"
  i=1
  while true; do
    echo "Running alembic upgrade head (attempt ${i}/${attempts})..."
    if alembic upgrade head; then
      echo "Migrations complete."
      break
    fi
    if [ "$i" -ge "$attempts" ]; then
      echo "Migrations failed after ${attempts} attempts." >&2
      exit 1
    fi
    i=$((i + 1))
    echo "Retrying in ${delay}s..."
    sleep "$delay"
  done
else
  echo "Skipping migrations (RUN_MIGRATIONS=${RUN_MIGRATIONS})."
fi

# Default to uvicorn when no command is provided (prevents empty-exec crash).
if [ "$#" -eq 0 ]; then
  set -- uvicorn app.main:app --host 0.0.0.0 --port 8000
fi

exec "$@"
