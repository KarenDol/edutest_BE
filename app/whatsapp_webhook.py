"""Meta WhatsApp Cloud API webhook.

Callback URL: {API origin}/auth/whatsapp/webhook (under /auth so the existing nginx routing reaches the API).
Docs: https://developers.facebook.com/docs/graph-api/webhooks/getting-started
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/whatsapp", tags=["whatsapp"])


@router.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """Meta's "Verify and save" handshake: echo hub.challenge when the verify token matches."""
    expected = (settings.whatsapp_verify_token or "").strip()
    if (
        not expected
        or mode != "subscribe"
        or verify_token is None
        or not hmac.compare_digest(verify_token, expected)
    ):
        raise HTTPException(status_code=403, detail="Verification failed")
    return challenge or ""


def _signature_ok(body: bytes, header: str | None) -> bool:
    secret = (settings.whatsapp_app_secret or "").strip()
    if not secret:
        logger.warning("WHATSAPP_APP_SECRET is not set: accepting WhatsApp webhook without signature check")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Incoming messages and delivery statuses. Meta retries on non-2xx, so only bad signatures are rejected."""
    body = await request.body()
    if not _signature_ok(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        return {"ok": True}
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for status in value.get("statuses") or []:
                logger.info(
                    "WhatsApp status: message %s -> %s for %s",
                    status.get("id"),
                    status.get("status"),
                    status.get("recipient_id"),
                )
            for message in value.get("messages") or []:
                logger.info("WhatsApp message from %s (type %s)", message.get("from"), message.get("type"))
    return {"ok": True}
