"""Minimal client for the Telegram Gateway API (verification codes sent via Telegram).

Docs: https://core.telegram.org/gateway/api
"""

import json
import logging
import re
import urllib.error
import urllib.request

from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://gatewayapi.telegram.org/"


class GatewayError(Exception):
    """Gateway returned ok=false (``code`` is Telegram's error string) or could not be reached."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def is_configured() -> bool:
    return bool((settings.telegram_gateway_token or "").strip())


def normalize_phone(raw: str) -> str | None:
    """Return E.164 (``+77011234567``) or None. Kazakh-style ``8 7xx …`` is mapped to ``+7 7xx …``."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("7"):
        digits = "7" + digits
    if not 10 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return "+" + digits


def _call(method: str, params: dict) -> dict:
    token = (settings.telegram_gateway_token or "").strip()
    req = urllib.request.Request(
        API_BASE + method,
        data=json.dumps(params).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        logger.warning("Telegram Gateway %s request failed: %s", method, e)
        raise GatewayError("NETWORK_ERROR") from e
    if not data.get("ok"):
        code = str(data.get("error") or "UNKNOWN_ERROR")
        logger.warning("Telegram Gateway %s error: %s", method, code)
        raise GatewayError(code)
    return data["result"]


def send_verification_message(phone: str, *, code_length: int = 6, ttl: int = 300) -> dict:
    """Telegram generates the code; returns a RequestStatus (``request_id`` etc.)."""
    return _call(
        "sendVerificationMessage",
        {"phone_number": phone, "code_length": code_length, "ttl": ttl},
    )


def check_verification_status(request_id: str, code: str) -> dict:
    """Returns a RequestStatus; ``verification_status.status`` is e.g. ``code_valid`` / ``code_invalid``."""
    return _call("checkVerificationStatus", {"request_id": request_id, "code": code})
