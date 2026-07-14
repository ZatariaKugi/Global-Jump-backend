"""Structured logging via structlog.

Console renderer in dev, JSON renderer in prod. A ``request_id`` (and any other
context bound by middleware) is merged into every log line via contextvars.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

from app.core.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging. Idempotent enough for app startup."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON
        # Keep local logs human-readable without padded spacing between fields.
        else structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
            pad_event=0,
            pad_level=False,
        )
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, etc.) through the same level.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
