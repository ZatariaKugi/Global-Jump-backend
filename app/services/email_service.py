"""Transactional email sending via fastapi-mail.

Falls back to structured logging when SMTP_HOST is not configured (local dev).
All emails are sent as multipart/alternative (plain-text + HTML) which is the
single most effective technique for avoiding spam filters.
"""

from __future__ import annotations

import io
import pathlib
from datetime import UTC, datetime

from aiosmtplib.errors import SMTPException
from fastapi import UploadFile
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
from fastapi_mail.errors import ConnectionErrors
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates" / "email"

_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, ctx: dict[str, object]) -> str:
    return str(_jinja.get_template(template_name).render(**ctx))


# SendGrid rewrites links/embeds tracking pixels by default, which routes mail through a
# redirect domain that spam filters score heavily — disable both via the SMTP relay's
# X-SMTPAPI header rather than requiring an account-level dashboard change.
_SENDGRID_NO_TRACKING_HEADERS = {
    "X-SMTPAPI": '{"filters":{"clicktrack":{"settings":{"enable":0}},'
    '"opentrack":{"settings":{"enable":0}}}}',
}


def _deliverability_headers(settings: Settings, *, reply_to: str | None = None) -> dict[str, str]:
    headers = dict(_SENDGRID_NO_TRACKING_HEADERS)
    headers["Reply-To"] = reply_to or settings.EMAILS_FROM
    return headers


def _make_connection(settings: Settings) -> ConnectionConfig:
    return ConnectionConfig(
        MAIL_USERNAME=settings.SMTP_USER or "",
        MAIL_PASSWORD=settings.SMTP_PASSWORD or "",
        MAIL_FROM=settings.EMAILS_FROM,
        MAIL_FROM_NAME=settings.EMAILS_FROM_NAME,
        MAIL_PORT=settings.SMTP_PORT,
        MAIL_SERVER=settings.SMTP_HOST or "localhost",
        MAIL_STARTTLS=settings.SMTP_TLS,
        MAIL_SSL_TLS=settings.SMTP_SSL,
        USE_CREDENTIALS=bool(settings.SMTP_USER),
        VALIDATE_CERTS=settings.is_production,
        TEMPLATE_FOLDER=str(_TEMPLATES_DIR),
    )


async def send_verification_email(
    to: str,
    full_name: str,
    raw_token: str,
    settings: Settings,
) -> None:
    verify_url = f"{settings.FRONTEND_URL}/verify-email/callback?token={raw_token}"
    ctx = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "verify_url": verify_url,
        "expire_hours": settings.EMAIL_VERIFY_TOKEN_EXPIRE_HOURS,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "email_verify_token_issued [no smtp — token logged]",
            to=to,
            verify_url=verify_url,
        )
        return

    html_body = _render("verify_email.html", ctx)
    text_body = _render("verify_email.txt", ctx)

    message = MessageSchema(
        subject=f"Verify your email address – {settings.EMAILS_FROM_NAME}",
        recipients=[to],
        body=html_body,
        subtype=MessageType.html,
        alternative_body=text_body,
        headers={
            # Inbox deliverability headers
            "X-Priority": "3",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            "List-Unsubscribe": f"<mailto:{settings.EMAILS_FROM}?subject=unsubscribe>",
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("verification_email_sent", to=to)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "verification_email_failed_smtp_unavailable",
            to=to,
            verify_url=verify_url,
            error=str(exc),
        )


async def send_advisor_welcome_email(
    to: str,
    full_name: str,
    settings: Settings,
) -> None:
    """Notify an advisor that their account has been approved."""
    login_url = f"{settings.FRONTEND_URL}/login"
    ctx = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "login_url": login_url,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "advisor_welcome_issued [no smtp — logged]",
            to=to,
            login_url=login_url,
        )
        return

    message = MessageSchema(
        subject=f"Welcome to {settings.EMAILS_FROM_NAME} — you're approved!",
        recipients=[to],
        body=_render("advisor_welcome.html", ctx),
        subtype=MessageType.html,
        alternative_body=_render("advisor_welcome.txt", ctx),
        headers={
            "X-Priority": "3",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            "List-Unsubscribe": f"<mailto:{settings.EMAILS_FROM}?subject=unsubscribe>",
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("advisor_welcome_email_sent", to=to)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "advisor_welcome_email_failed_smtp_unavailable",
            to=to,
            error=str(exc),
        )


async def send_advisor_rejected_email(
    to: str,
    full_name: str,
    settings: Settings,
    *,
    reason: str | None = None,
) -> None:
    """Notify an advisor that their account application was rejected."""
    login_url = f"{settings.FRONTEND_URL}/login"
    ctx: dict[str, object] = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "reason": reason,
        "login_url": login_url,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "advisor_rejected_issued [no smtp — logged]",
            to=to,
            reason=reason,
            login_url=login_url,
        )
        return

    message = MessageSchema(
        subject=f"Your {settings.EMAILS_FROM_NAME} advisor application",
        recipients=[to],
        body=_render("advisor_rejected.html", ctx),
        subtype=MessageType.html,
        alternative_body=_render("advisor_rejected.txt", ctx),
        headers={
            "X-Priority": "3",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            "List-Unsubscribe": f"<mailto:{settings.EMAILS_FROM}?subject=unsubscribe>",
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("advisor_rejected_email_sent", to=to)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "advisor_rejected_email_failed_smtp_unavailable",
            to=to,
            error=str(exc),
        )


async def send_advisor_pending_email(
    to: str,
    full_name: str,
    settings: Settings,
) -> None:
    """Notify an advisor that their application is pending review."""
    login_url = f"{settings.FRONTEND_URL}/login"
    ctx = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "login_url": login_url,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "advisor_pending_issued [no smtp — logged]",
            to=to,
            login_url=login_url,
        )
        return

    message = MessageSchema(
        subject=f"Your {settings.EMAILS_FROM_NAME} application is pending review",
        recipients=[to],
        body=_render("advisor_pending.html", ctx),
        subtype=MessageType.html,
        alternative_body=_render("advisor_pending.txt", ctx),
        headers={
            "X-Priority": "3",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            "List-Unsubscribe": f"<mailto:{settings.EMAILS_FROM}?subject=unsubscribe>",
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("advisor_pending_email_sent", to=to)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "advisor_pending_email_failed_smtp_unavailable",
            to=to,
            error=str(exc),
        )


async def send_password_reset_email(
    to: str,
    full_name: str,
    raw_token: str,
    settings: Settings,
) -> None:
    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={raw_token}"
    ctx = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "email": to,
        "reset_url": reset_url,
        "expire_minutes": settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "password_reset_token_issued [no smtp — token logged]",
            to=to,
            reset_url=reset_url,
        )
        return

    html_body = _render("reset_password.html", ctx)
    text_body = _render("reset_password.txt", ctx)

    message = MessageSchema(
        subject=f"Reset your {settings.EMAILS_FROM_NAME} password",
        recipients=[to],
        body=html_body,
        subtype=MessageType.html,
        alternative_body=text_body,
        headers={
            "X-Priority": "1",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            "List-Unsubscribe": f"<mailto:{settings.EMAILS_FROM}?subject=unsubscribe>",
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("password_reset_email_sent", to=to)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "password_reset_email_failed_smtp_unavailable",
            to=to,
            reset_url=reset_url,
            error=str(exc),
        )


async def send_payment_receipt_email(
    to: str,
    full_name: str,
    advisor_name: str,
    *,
    service_type: str,
    amount_usd: float,
    invoice_number: str,
    settings: Settings,
) -> None:
    ctx = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "advisor_name": advisor_name,
        "service_type": service_type,
        "amount_usd": f"{amount_usd:.2f}",
        "invoice_number": invoice_number,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "payment_receipt_issued [no smtp — logged]",
            to=to,
            invoice_number=invoice_number,
        )
        return

    try:
        message = MessageSchema(
            subject=f"Payment receipt – {settings.EMAILS_FROM_NAME}",
            recipients=[to],
            body=_render("payment_receipt.html", ctx),
            subtype=MessageType.html,
            alternative_body=_render("payment_receipt.txt", ctx),
            headers={
                "X-Priority": "3",
                "X-Mailer": settings.EMAILS_FROM_NAME,
                **_deliverability_headers(settings),
            },
        )
    except ValidationError as exc:
        # Reserved TLDs (e.g. .test seed data) fail pydantic email validation.
        logger.warning(
            "payment_receipt_failed_invalid_recipient",
            to=to,
            invoice_number=invoice_number,
            error=str(exc),
        )
        return

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("payment_receipt_sent", to=to, invoice_number=invoice_number)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "payment_receipt_failed_smtp_unavailable",
            to=to,
            invoice_number=invoice_number,
            error=str(exc),
        )


def build_ics(
    *,
    uid: str,
    summary: str,
    description: str,
    start_utc: datetime,
    end_utc: datetime,
) -> str:
    """Minimal RFC 5545 VEVENT — enough for calendar clients to import."""

    def _fmt(dt: datetime) -> str:
        return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//GlobleJump//Booking//EN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{_fmt(datetime.now(UTC))}\r\n"
        f"DTSTART:{_fmt(start_utc)}\r\n"
        f"DTEND:{_fmt(end_utc)}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DESCRIPTION:{description}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


async def send_booking_confirmation_email(
    to: str,
    full_name: str,
    other_party: str,
    *,
    booking_id: str,
    service_type: str,
    start_utc: datetime,
    end_utc: datetime,
    duration_minutes: int,
    price_usd: float,
    notice_hours: int,
    settings: Settings,
) -> None:
    """Booking confirmation with a ``booking.ics`` calendar attachment."""
    ctx = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "other_party": other_party,
        "service_type": service_type,
        "start_str": start_utc.astimezone(UTC).strftime("%A, %d %B %Y at %H:%M"),
        "duration_minutes": duration_minutes,
        "price_usd": f"{price_usd:.2f}",
        "notice_hours": notice_hours,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "booking_confirmation_issued [no smtp — logged]",
            to=to,
            booking_id=booking_id,
            start_utc=str(start_utc),
        )
        return

    ics_content = build_ics(
        uid=f"{booking_id}@globlejump",
        summary=f"{settings.EMAILS_FROM_NAME} consultation: {service_type}",
        description=f"Consultation with {other_party}",
        start_utc=start_utc,
        end_utc=end_utc,
    )
    ics_upload = UploadFile(
        filename="booking.ics",
        file=io.BytesIO(ics_content.encode()),
        headers=Headers({"content-type": "text/calendar"}),
    )

    message = MessageSchema(
        subject=f"Booking confirmed – {settings.EMAILS_FROM_NAME}",
        recipients=[to],
        body=_render("booking_confirmation.html", ctx),
        subtype=MessageType.html,
        alternative_body=_render("booking_confirmation.txt", ctx),
        attachments=[
            {
                "file": ics_upload,
                "headers": {
                    "Content-Disposition": 'attachment; filename="booking.ics"',
                },
                "mime_type": "text",
                "mime_subtype": "calendar",
            }
        ],
        headers={
            "X-Priority": "3",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("booking_confirmation_sent", to=to, booking_id=booking_id)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "booking_confirmation_failed_smtp_unavailable",
            to=to,
            booking_id=booking_id,
            error=str(exc),
        )


async def send_new_consultation_request_email(
    to: str,
    full_name: str,
    other_party: str,
    *,
    booking_id: str,
    service_type: str,
    start_utc: datetime,
    settings: Settings,
) -> None:
    """Notify an advisor that a new consultation request is awaiting accept/reject."""
    ctx = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "other_party": other_party,
        "service_type": service_type,
        "start_str": start_utc.astimezone(UTC).strftime("%A, %d %B %Y at %H:%M"),
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "new_consultation_request_issued [no smtp — logged]",
            to=to,
            booking_id=booking_id,
        )
        return

    message = MessageSchema(
        subject=f"New consultation request – {settings.EMAILS_FROM_NAME}",
        recipients=[to],
        body=_render("new_consultation_request.html", ctx),
        subtype=MessageType.html,
        alternative_body=_render("new_consultation_request.txt", ctx),
        headers={
            "X-Priority": "3",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("new_consultation_request_sent", to=to, booking_id=booking_id)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "new_consultation_request_failed_smtp_unavailable",
            to=to,
            booking_id=booking_id,
            error=str(exc),
        )


async def send_booking_rejected_email(
    to: str,
    full_name: str,
    other_party: str,
    *,
    booking_id: str,
    service_type: str,
    start_utc: datetime,
    reason: str | None,
    settings: Settings,
) -> None:
    """Notify a seeker that their consultation request was declined."""
    ctx: dict[str, object] = {
        "app_name": settings.EMAILS_FROM_NAME,
        "full_name": full_name or to,
        "other_party": other_party,
        "service_type": service_type,
        "start_str": start_utc.astimezone(UTC).strftime("%A, %d %B %Y at %H:%M"),
        "reason": reason,
        "year": datetime.now(UTC).year,
    }

    if not settings.SMTP_HOST:
        logger.info(
            "booking_rejected_issued [no smtp — logged]",
            to=to,
            booking_id=booking_id,
        )
        return

    message = MessageSchema(
        subject=f"Consultation request declined – {settings.EMAILS_FROM_NAME}",
        recipients=[to],
        body=_render("booking_rejected.html", ctx),
        subtype=MessageType.html,
        alternative_body=_render("booking_rejected.txt", ctx),
        headers={
            "X-Priority": "3",
            "X-Mailer": settings.EMAILS_FROM_NAME,
            **_deliverability_headers(settings),
        },
    )

    try:
        fm = FastMail(_make_connection(settings))
        await fm.send_message(message)
        logger.info("booking_rejected_sent", to=to, booking_id=booking_id)
    except (ConnectionErrors, OSError, SMTPException) as exc:
        logger.warning(
            "booking_rejected_failed_smtp_unavailable",
            to=to,
            booking_id=booking_id,
            error=str(exc),
        )
