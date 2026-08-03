"""FastAPI application factory and entrypoint."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.api.v1 import health
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.observability import init_sentry, init_tracing, instrument_metrics
from app.core.scheduler import create_scheduler
from app.db.session import engine
from app.middleware.request_context import RequestContextMiddleware
from app.services import push_service

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info("startup", environment=settings.ENVIRONMENT.value)
    init_tracing(app, settings, engine=engine)
    push_service.init_firebase(settings)
    scheduler = create_scheduler(settings)
    scheduler.start()
    logger.info(
        "scheduler_started",
        sweep_seconds=settings.TRANSFER_SWEEP_SECONDS,
        push_sweep_seconds=settings.NOTIFICATION_PUSH_SWEEP_SECONDS,
        push_enabled=settings.push_enabled,
    )
    yield
    scheduler.shutdown(wait=False)
    await engine.dispose()
    logger.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    init_sentry(settings)

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    # Middleware is applied outermost-last. CORS must sit outside metrics /
    # request-id so OPTIONS preflight is answered with ACAO headers (and not
    # turned into a bare 405 from the route). allow_private_network is required
    # when the FE is on localhost and the API is on a LAN IP (Chrome PNA).
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    instrument_metrics(app)

    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(o) for o in settings.CORS_ORIGINS],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_private_network=True,
        )

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

    app.include_router(health.router)  # /health, /readiness at root
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
