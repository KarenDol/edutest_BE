"""Minimal client for the Cascade OTP API (codes via Telegram / WhatsApp; Cascade generates and checks them).

Docs: https://docs.cascade.kz
"""

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://cascade.kz/api/"


class CascadeError(Exception):
    """Cascade returned success=false or could not be reached.

    ``status`` is the HTTP status (0 = network error), ``message`` is Cascade's (Russian) user-facing text.
    """

    def __init__(self, status: int, message: str, error_code: str | None = None):
        super().__init__(f"{status} {error_code}: {message}")
        self.status = status
        self.message = message
        self.error_code = error_code


def is_configured() -> bool:
    return bool((settings.cascade_api_key or "").strip())


def _client_domain() -> str:
    """The API key is bound to a domain, sent as X-Cascade-Client-Domain."""
    explicit = (settings.cascade_client_domain or "").strip()
    if explicit:
        return explicit
    return urlparse((settings.public_app_base_url or "").strip()).hostname or ""


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {(settings.cascade_api_key or '').strip()}",
            "X-Cascade-Client-Domain": _client_domain(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning("Cascade %s request failed: %s", path, e)
        raise CascadeError(0, "network error") from e
    try:
        data = json.loads(raw.decode() or "{}")
    except ValueError:
        data = {}
    if status == 200 and data.get("success"):
        return data
    message = str(data.get("message") or f"HTTP {status}")
    error_code = data.get("error_code")
    logger.warning("Cascade %s error: HTTP %s %s %s", path, status, error_code, message)
    raise CascadeError(status, message, error_code)


def _cascade_phone(phone: str) -> str:
    """Cascade expects digits only (``77001234567``)."""
    return phone.lstrip("+")


def send_otp(phone: str, purpose: str) -> dict:
    """Returns e.g. ``{"otp_id": 1241, "channel": "telegram", "expires_in": 300, ...}``."""
    return _post(
        "otp/send",
        {
            "phone": _cascade_phone(phone),
            "purpose": purpose,
            "channel": settings.cascade_channel,
            "locale": settings.cascade_locale,
        },
    )


def verify_otp(phone: str, purpose: str, code: str) -> dict:
    return _post("otp/verify", {"phone": _cascade_phone(phone), "purpose": purpose, "code": code})
