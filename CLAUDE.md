# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands are available via `make`. The `uv` package manager is used — never use `pip` directly.

```bash
make run          # dev server with hot reload (0.0.0.0:8000)
make test         # run full test suite
make check        # lint + typecheck + tests in one shot
make lint         # ruff check
make typecheck    # mypy app/
make format       # ruff format (auto-fix)
make migrate      # alembic upgrade head
make migrate-create MSG="describe_change"  # autogenerate new migration
make migrate-downgrade  # roll back one revision
make db-up        # start postgres via docker compose
```

Run a single test file:
```bash
uv run pytest tests/test_auth.py -v
```

Run a single test by name:
```bash
uv run pytest tests/test_auth.py::test_login_and_me_happy_path -v
```

Alembic without `uv run` (if uv network unavailable):
```bash
.venv/bin/alembic upgrade head
```

Do not write or run pytest tests, and do not run the test suite to verify changes, unless the user explicitly asks for it. Rely on lint (`make lint`) and typecheck (`make typecheck`) instead.

## Architecture

### Request lifecycle

```
Request
  → RequestContextMiddleware  (attaches request_id to structlog context)
  → CORSMiddleware
  → Route handler
      → SessionDep             (AsyncSession, commits on success / rolls back on error)
      → SettingsDep            (lru_cache singleton from .env)
      → get_current_principal  (decodes JWT, resolves User from DB)
  → ResponseEnvelope[T]        (all success responses wrapped in {data, meta})
  → Exception handlers         (AppError subclasses → JSON error envelope)
```

### Authentication model

Two JWT issuers are trusted simultaneously:
- **Local** (`JWT_ISSUER=globlejump`): HS256 tokens issued by this service's `/auth/login`
- **External** (`IDENTITY_ISSUER`): tokens from an external identity service (JWKS or shared secret)

`get_current_principal` in `app/api/deps.py` resolves both to a `Principal` dataclass. External principals have no `User` ORM object. `get_current_user` (used by most endpoints) rejects external-only principals.

### Three-role RBAC

Roles: `customer`, `advisor`, `admin`. Enforced via:
- `require_role(UserRole.admin)` — dependency factory, used as router-level dependency on `/admin`
- `require_verified_advisor` — dependency for advisor-only endpoints; checks both role and `verification_status == approved`

Advisors register as inactive (`is_active=False`, `verification_status=pending`). Login is blocked until an admin approves via `PATCH /admin/advisors/{id}/verification`.

### Token security

- **Access token**: short-lived JWT (HS256), never stored
- **Refresh token**: `secrets.token_urlsafe(32)` raw token returned to client; only its SHA-256 hash stored in `refresh_tokens` table; rotated on every `/auth/refresh` call
- **One-time tokens** (email verify, password reset): same hash pattern via `user_tokens` table with `purpose` discriminator; single-use enforced by `used_at` timestamp

Raw tokens are **never** persisted. Only `hash_token(raw)` (SHA-256 hex) goes to the DB.

### Response envelope

Every endpoint returns `ResponseEnvelope[T]` — `{success: true, data: T, meta: {request_id, timestamp, pagination}}` where `pagination` (list endpoints only) is `{total, page, page_size, pages}`. Error responses follow `{success: false, error: {code, message, detail}, meta: {request_id, timestamp}}`. Both shapes are defined in `app/schemas/response.py` and `app/core/exceptions.py` respectively.

### Database

- SQLAlchemy 2.0 async with `asyncpg`. All models extend `BaseModel` (`app/db/base_model.py`) which provides `id` (UUID), `created_at`, `updated_at`, `created_by`, `updated_by`, `is_archived`.
- **Never use `sa.JSON` or `sa.JSONB` columns in ORM models.** All multi-valued data must be normalised into child tables with a FK to the parent. Child models that don't need audit columns can inherit directly from `Base` (not `BaseModel`) with just an `id` UUID primary key. Use `lazy="selectin"` on relationships for async compatibility.
- Alembic migrations live in `migrations/versions/`. PostgreSQL enum types must be created explicitly before `ALTER TABLE` — see existing migrations for the pattern.
- The test suite uses **SQLite in-memory** via `aiosqlite` (no Postgres needed). The `engine` and `client` fixtures in `tests/conftest.py` handle setup and dependency override.

### Profile models and child tables

`CustomerProfile` and `AdvisorProfile` use normalised child tables instead of JSON columns:

| Parent | Child table | Relationship attr |
|--------|-------------|-------------------|
| `CustomerProfile` | `customer_countries_visited` | `countries_visited` |
| `CustomerProfile` | `customer_prior_visas` | `prior_visas` |
| `AdvisorProfile` | `advisor_visa_specializations` | `visa_specializations` |
| `AdvisorProfile` | `advisor_country_expertise` | `country_expertise` |
| `AdvisorProfile` | `advisor_languages` | `languages` |
| `AdvisorProfile` | `advisor_services` | `services` |

All relationships use `cascade="all, delete-orphan"`. Services replace the entire child collection on update (reassigning the relationship list; delete-orphan cascade removes the old rows).

### Sensitive field encryption

Passport numbers use AES-256-GCM via `app/core/encryption.py`. `encrypt_field(plaintext, settings)` / `decrypt_field(ciphertext_b64, settings)` — key is `ENCRYPTION_KEY` (base64url-encoded 32 bytes). Plaintext is **never** stored; only the masked last-4 chars are returned in API responses.

### File uploads

`app/core/file_storage.py` — validates extension (pdf/jpg/jpeg/png) and size (≤ `UPLOAD_MAX_MB`), writes with `aiofiles` to `{UPLOAD_DIR}/{subdir}/{uuid}{ext}`, returns `(url_path, size_bytes)`. Files served as static at `/uploads`.

### Availability & bookings

Advisor availability is stored as weekly recurring slots (`advisor_weekly_slots`: weekday + advisor-local `Time` + IANA timezone string) plus one-off blocked dates (`advisor_availability_overrides`). All booking instants are stored UTC; `availability_service.free_slots()` expands weekly slots to UTC per date via `zoneinfo` (DST-correct), subtracts blocked days and active bookings, and chops into duration-sized increments. `booking_service.create()` validates the requested start against `free_slots` — this doubles as the double-booking check. Bookings snapshot the service type/duration/price at creation. Customer cancellations/reschedules enforce `advisor_profiles.cancellation_notice_hours` (default 24); advisors may act any time. `as_utc()` in `availability_service` normalises SQLite's naive datetimes — use it when comparing stored datetimes.

### In-platform messaging

`Conversation` is one thread per (customer, advisor) pair (`UniqueConstraint("customer_id", "advisor_id")`), created via `conversation_service.get_or_create()` which requires an existing `Booking` between the two users (any status) — otherwise `PermissionDeniedError`. `Message` belongs to a conversation, has an optional `body` (String(5000)) and/or `MessageAttachment` child rows (plain `Base`, no audit columns); a message must have a body or at least one attachment. `Message.created_at` is set explicitly in `send_message()` (not left to the DB `server_default`) so ordering/"last message" lookups are unambiguous even at SQLite's one-second timestamp resolution.

Messages reuse the `moderation_status` enum/column pattern from reviews (`ModerationStatus`: `visible`/`flagged`/`removed`, `app/models/review.py`) — the messaging migration sets `create_type=False` since the Postgres enum type already exists. `PUBLIC_STATUSES = (visible, flagged)` controls what's returned from `list_messages_stmt`; a `removed` message disappears from the conversation. Reporting (`POST /messages/{id}/report`) sets `flagged` + `flag_reason` + `flagged_by`; admin moderation (`GET /admin/messages/flagged`, `PATCH /admin/messages/{id}/moderation`) mirrors the reviews endpoints and reuses `ModerationDecision` from `app.schemas.review`.

Real-time delivery uses `app/services/ws_manager.py`'s in-memory `ConnectionManager` (per-conversation `set[WebSocket]`, single-process only). The WebSocket route `/conversations/{id}/ws` authenticates manually — it calls `websocket.accept()` first, then reads the bearer token from the `Authorization` header (or `token` query param) and decodes it directly, because `CurrentUser`'s dependency-chain exceptions can't be turned into a clean `websocket.close()` if raised before `accept()`. New messages sent via the REST endpoint are broadcast as `{"type": "message", "data": ...}`; read receipts (`PATCH /messages/{id}/read` or a `{"type": "read", "message_id": ...}` WS frame) broadcast `{"type": "read", ...}`.

### Email

`app/services/email_service.py` sends via `fastapi-mail`. When `SMTP_HOST` is unset, it logs the token URL instead of sending (dev fallback). SMTP errors are caught and logged as warnings — they never cause a 500.

Templates are Jinja2 HTML+text pairs in `app/templates/email/`. Variables: `app_name`, `full_name`, `verify_url`/`reset_url`, `expire_hours`/`expire_minutes`, `year`.

### Key config values

`FRONTEND_URL` is used to build token links in emails (e.g. `{FRONTEND_URL}/auth/verify-email?token=...`). `JWT_ISSUER` must match what the frontend sends — currently `globlejump`. Database name in production is `jumpdb`.

### Adding a new endpoint

1. Add schema to `app/schemas/`
2. Add service function to `app/services/`
3. Add route to relevant `app/api/v1/` router; use `ResponseEnvelope[YourSchema]` as `response_model`
4. If new DB columns or tables: `make migrate-create MSG="describe"`, then review the generated file for enum type ordering issues before applying
5. Multi-valued fields → normalised child tables (never JSON columns)
