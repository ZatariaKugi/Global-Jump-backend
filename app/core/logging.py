"""Structured logging via structlog.

Console renderer in dev (colorama-colored levels), JSON renderer in prod.
A ``request_id`` (and any other context bound by middleware) is merged into
every log line via contextvars. Stdlib loggers (uvicorn, httpx, etc.) use the
same colorama level colors.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog
from colorama import Fore, Style, just_fix_windows_console

from app.core.config import Settings

# Enable ANSI colors on Windows terminals; no-op elsewhere.
just_fix_windows_console()

# Full-line colors by level: errors red, warnings yellow, success/info green.
_LEVEL_STYLES = {
    "critical": Fore.RED + Style.BRIGHT,
    "error": Fore.RED + Style.BRIGHT,
    "exception": Fore.RED + Style.BRIGHT,
    "warn": Fore.YELLOW + Style.BRIGHT,
    "warning": Fore.YELLOW + Style.BRIGHT,
    "info": Fore.GREEN,
    "debug": Fore.CYAN,
    "notset": Style.RESET_ALL,
}

_STDLIB_LEVEL_COLORS = {
    logging.CRITICAL: Fore.RED + Style.BRIGHT,
    logging.ERROR: Fore.RED + Style.BRIGHT,
    logging.WARNING: Fore.YELLOW + Style.BRIGHT,
    logging.INFO: Fore.GREEN,
    logging.DEBUG: Fore.CYAN,
}


class ColoramaLogFormatter(logging.Formatter):
    """Colorize the full stdlib log line (uvicorn ``WARNING: …``, httpx, etc.)."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = _STDLIB_LEVEL_COLORS.get(record.levelno, "")
        if not color:
            return message
        return f"{color}{message}{Style.RESET_ALL}"


class DropWebSocketAccessFilter(logging.Filter):
    """Drop uvicorn lines for WebSocket upgrades.

    WebSocket accept/reject is logged on ``uvicorn.error`` (not access) and
    includes the raw ``?token=…`` query string. Regular HTTP access logs are kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return '"WebSocket ' not in message


class _ColoredConsoleRenderer:
    """ConsoleRenderer wrapper that paints the whole line by log level."""

    def __init__(self) -> None:
        self._inner = structlog.dev.ConsoleRenderer(
            colors=True,
            force_colors=True,
            level_styles=_LEVEL_STYLES,
            pad_event_to=False,
            pad_level=False,
        )

    def __call__(self, logger: Any, method_name: str, event_dict: dict[str, Any]) -> str:
        level = str(event_dict.get("level", method_name)).lower()
        line = self._inner(logger, method_name, event_dict)
        color = _LEVEL_STYLES.get(level, "")
        if not color:
            return line
        return f"{color}{line}{Style.RESET_ALL}"


def _configure_stdlib(level: int, *, colored: bool) -> None:
    """Replace root handlers so uvicorn / httpx lines pick up colorama."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    fmt = "%(levelname)s:     %(message)s"
    handler.setFormatter(
        ColoramaLogFormatter(fmt) if colored else logging.Formatter("%(message)s")
    )
    for existing in list(handler.filters):
        if isinstance(existing, DropWebSocketAccessFilter):
            handler.removeFilter(existing)
    handler.addFilter(DropWebSocketAccessFilter())
    root.addHandler(handler)

    # Uvicorn installs its own handlers; force them onto the colored root path.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(level)

    logging.getLogger("uvicorn.access").setLevel(max(level, logging.INFO))


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging. Idempotent enough for app startup."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    colored = not settings.LOG_JSON

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if settings.LOG_JSON:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = cast(structlog.types.Processor, _ColoredConsoleRenderer())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configure_stdlib(level, colored=colored)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
