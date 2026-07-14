"""Observability wiring: Prometheus metrics, Sentry, and OpenTelemetry.

Each integration is optional and activated only when its configuration is present, so
the boilerplate runs out-of-the-box locally and lights up in production via env vars.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def init_sentry(settings: Settings) -> None:
    if not settings.SENTRY_DSN:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT.value,
        traces_sample_rate=0.1 if settings.is_production else 1.0,
        send_default_pii=False,
    )
    logger.info("sentry_initialized")


def instrument_metrics(app: FastAPI) -> None:
    """Expose Prometheus metrics at /metrics."""
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator(
        should_group_status_codes=True,
        excluded_handlers=["/metrics", "/health", "/readiness"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


def init_tracing(app: FastAPI, settings: Settings, engine: object | None = None) -> None:
    """Configure OpenTelemetry tracing + auto-instrumentation when an OTLP endpoint is set."""
    if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT))
    )
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,readiness,metrics")
    if engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        # AsyncEngine exposes the underlying sync engine via .sync_engine
        sync_engine = getattr(engine, "sync_engine", engine)
        SQLAlchemyInstrumentor().instrument(engine=sync_engine)

    logger.info("tracing_initialized", endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
