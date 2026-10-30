"""Send email via SMTP (HTML messages for optional staff alerts and student verification)."""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


def _smtp_configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def log_email_startup_hints(settings: Settings) -> None:
    """Log hints if mail notify is requested but SMTP is not usable."""
    if settings.email_approval_notify_recipients and not _smtp_configured(settings):
        logger.warning(
            "EMAIL_APPROVAL_NOTIFY_TO is set but SMTP is incomplete: set SMTP_HOST and SMTP_FROM "
            "(and usually SMTP_USER / SMTP_PASSWORD). See .env.example."
        )
    if _smtp_configured(settings) and not (settings.public_app_base_url or "").strip():
        logger.warning(
            "PUBLIC_APP_BASE_URL is empty: student email verification links cannot be built. "
            "Set it to your frontend origin (e.g. https://app.example.com)."
        )


def send_mail_html(
    settings: Settings,
    *,
    to_addresses: list[str],
    subject: str,
    html_body: str,
) -> bool:
    """Send multipart/alternative (plain + HTML) via SMTP. Returns True on success."""
    if not _smtp_configured(settings):
        return False
    cleaned = [a.strip() for a in to_addresses if a.strip()]
    if not cleaned:
        return False

    host = settings.smtp_host.strip()
    port = int(settings.smtp_port)
    use_ssl = settings.smtp_use_ssl
    use_tls = settings.smtp_use_tls and not use_ssl
    user = (settings.smtp_user or "").strip() or None
    # Google “app passwords” are often copied with spaces; Gmail expects 16 chars without them.
    password = (settings.smtp_password or "").replace(" ", "").replace("\xa0", "")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from.strip()
    msg["To"] = ", ".join(cleaned)
    plain_fallback = "This message requires an HTML-capable mail client."
    msg.set_content(plain_fallback)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as smtp:
                if user is not None:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls(context=ctx)
                    smtp.ehlo()
                if user is not None:
                    smtp.login(user, password)
                smtp.send_message(msg)
        return True
    except OSError as e:
        logger.warning("SMTP send failed: %s", e)
        return False


def notify_admins_new_student(
    settings: Settings,
    *,
    student_name: str,
    student_phone: str,
    user_id: str,
) -> None:
    recipients = settings.email_approval_notify_recipients
    if not recipients:
        return
    if not _smtp_configured(settings):
        logger.warning(
            "EMAIL_APPROVAL_NOTIFY_TO is set but SMTP is not configured (SMTP_HOST, SMTP_FROM). "
            "Skipping admin notification."
        )
        return

    safe_name = html.escape(student_name)
    safe_phone = html.escape(student_phone)
    safe_id = html.escape(user_id)
    base = (settings.public_app_base_url or "").rstrip("/")
    link_line = ""
    if base:
        sb = html.escape(base)
        link_line = f'<p><strong>App:</strong> <a href="{sb}">{sb}</a></p>'

    subject = "[EduTest by Narxoz] New student registered"
    intro = (
        "<p>A new student registered with a phone number confirmed via Telegram.</p>"
    )

    body = f"""<html><body>
{intro}
<ul>
  <li><strong>Name:</strong> {safe_name}</li>
  <li><strong>Phone:</strong> {safe_phone}</li>
  <li><strong>User id:</strong> {safe_id}</li>
</ul>
{link_line}
</body></html>"""
    ok = send_mail_html(
        settings,
        to_addresses=recipients,
        subject=subject,
        html_body=body,
    )
    if not ok:
        logger.warning("Failed to send admin signup notification to %s", recipients)


def notify_student_email_verification(
    settings: Settings,
    *,
    student_email: str,
    student_name: str,
    verification_link: str,
) -> bool:
    """Send the student a link to confirm they own the email address."""
    safe_name = html.escape(student_name)
    safe_link = html.escape(verification_link)
    body = f"""<html><body>
<p>Hello {safe_name},</p>
<p>Please confirm your email address for <strong>EduTest by Narxoz</strong> by clicking the link below:</p>
<p><a href="{safe_link}">Confirm my email</a></p>
<p>If you did not create an account, you can ignore this message.</p>
</body></html>"""
    return send_mail_html(
        settings,
        to_addresses=[student_email],
        subject="[EduTest by Narxoz] Confirm your email",
        html_body=body,
    )
