"""Phone verification codes: Cascade when CASCADE_API_KEY is set, otherwise the Telegram Gateway."""

from typing import Literal

from app import cascade_otp, telegram_gateway

CheckResult = Literal["valid", "invalid", "expired"]


class OtpSendError(Exception):
    """``user_message`` is safe to show (None = provider problem, show a generic message)."""

    def __init__(self, user_message: str | None = None, *, rate_limited: bool = False):
        super().__init__(user_message or "provider error")
        self.user_message = user_message
        self.rate_limited = rate_limited


class OtpCheckError(Exception):
    """Provider unreachable / misconfigured while checking a code."""


def is_configured() -> bool:
    return cascade_otp.is_configured() or telegram_gateway.is_configured()


def send_code(phone: str, purpose: str, ttl: int) -> str:
    """Send a code; returns the provider's request id."""
    if cascade_otp.is_configured():
        try:
            result = cascade_otp.send_otp(phone, purpose)
        except cascade_otp.CascadeError as e:
            # 422/429 and 503 delivery_failed carry a user-facing reason (bad number, wait before resend,
            # number has no Telegram / blocks messages, …).
            if e.status in (422, 429) or e.error_code == "delivery_failed":
                raise OtpSendError(e.message, rate_limited=e.status == 429) from None
            raise OtpSendError() from None
        return str(result.get("otp_id") or "")

    try:
        result = telegram_gateway.send_verification_message(phone, ttl=ttl)
    except telegram_gateway.GatewayError as e:
        if e.code.startswith("PHONE_NUMBER"):
            raise OtpSendError(
                "Could not send a code to this number. Check the number and make sure it has a Telegram account."
            ) from None
        raise OtpSendError() from None
    return str(result["request_id"])


def check_code(request_id: str, phone: str, purpose: str, code: str) -> CheckResult:
    if cascade_otp.is_configured():
        try:
            cascade_otp.verify_otp(phone, purpose, code)
        except cascade_otp.CascadeError as e:
            if e.status == 422:
                return "invalid"
            if e.status == 429:
                return "expired"
            raise OtpCheckError() from None
        return "valid"

    try:
        result = telegram_gateway.check_verification_status(request_id, code)
    except telegram_gateway.GatewayError:
        raise OtpCheckError() from None
    status = (result.get("verification_status") or {}).get("status")
    if status == "code_valid":
        return "valid"
    if status == "code_invalid":
        return "invalid"
    return "expired"
