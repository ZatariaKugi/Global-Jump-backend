#!/bin/sh
# Shared DB readiness wait — TCP connect to DATABASE_URL host/port.
# Avoids crashing alembic/uvicorn when Postgres is still accepting connections
# but not ready for auth / first queries.
set -eu

python - <<'PY'
import os
import socket
import sys
import time
from urllib.parse import urlparse

raw = os.environ.get("DATABASE_URL") or ""
if not raw:
    # Fall back to discrete POSTGRES_* if DATABASE_URL unset.
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
else:
    normalized = raw.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlparse(normalized)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432

attempts = int(os.environ.get("DB_WAIT_ATTEMPTS", "60"))
delay = float(os.environ.get("DB_WAIT_DELAY_SEC", "1"))

for i in range(1, attempts + 1):
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"Database reachable at {host}:{port} (attempt {i})", flush=True)
            sys.exit(0)
    except OSError as exc:
        print(f"Waiting for database {host}:{port} ({i}/{attempts}): {exc}", flush=True)
        time.sleep(delay)

print(f"Database not reachable at {host}:{port} after {attempts} attempts", flush=True)
sys.exit(1)
PY
