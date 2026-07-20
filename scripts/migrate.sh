#!/bin/sh
# One-shot migration runner for Compose / CI (no uvicorn CMD appended).
set -eu

echo "Waiting for database before migrations..."
/app/scripts/wait_for_db.sh

attempts="${MIGRATE_ATTEMPTS:-10}"
delay="${MIGRATE_RETRY_DELAY_SEC:-3}"
i=1
while true; do
  echo "Running alembic upgrade head (attempt ${i}/${attempts})..."
  if alembic upgrade head; then
    echo "Migrations complete."
    exit 0
  fi
  if [ "$i" -ge "$attempts" ]; then
    echo "Migrations failed after ${attempts} attempts." >&2
    exit 1
  fi
  i=$((i + 1))
  echo "Retrying in ${delay}s..."
  sleep "$delay"
done
