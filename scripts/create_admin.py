"""Create (or update the password of) an admin account.

There is deliberately no API endpoint for creating admins — public registration
always creates a seeker, and admin endpoints exclude admin accounts entirely.
Bootstrap the first admin with this script instead.

Run with:
    uv run python -m scripts.create_admin admin@example.com
    uv run python -m scripts.create_admin admin@example.com --full-name "Site Admin"

The password is read from the ADMIN_PASSWORD environment variable, or prompted
for interactively when unset. Idempotent: re-running for an existing admin
resets its password; a non-admin user with the same email is never escalated.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.user import User, UserRole

logger = get_logger(__name__)

MIN_PASSWORD_LENGTH = 8


def _read_password() -> str:
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


async def create_admin(email: str, password: str, full_name: str | None) -> None:
    async with async_session_factory() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            if existing.role != UserRole.admin:
                sys.exit(
                    f"A {existing.role.value} account already uses {email}; "
                    "refusing to escalate it to admin."
                )
            existing.hashed_password = hash_password(password)
            await session.commit()
            logger.info("admin_password_reset", email=email)
            return

        session.add(
            User(
                email=email,
                full_name=full_name,
                hashed_password=hash_password(password),
                role=UserRole.admin,
                is_active=True,
                email_verified_at=datetime.now(UTC),
            )
        )
        await session.commit()
        logger.info("admin_created", email=email)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="Admin login email")
    parser.add_argument("--full-name", default=None, help="Display name (optional)")
    args = parser.parse_args()
    try:
        await create_admin(args.email.strip().lower(), _read_password(), args.full_name)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
