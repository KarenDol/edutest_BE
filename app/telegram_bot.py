"""@edutest_narxoz_bot: students verify their phone by sharing their Telegram contact.

Flow: the site calls /auth/telegram/start and opens t.me/<bot>?start=<nonce>; the bot asks for the contact,
checks it is the sender's own number, and completes the TelegramBotSession with a verified PhoneVerification.

Run (long polling, no inbound webhook needed): ``python -m app.telegram_bot``
"""

import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import PhoneVerification, TelegramBotSession, User
from app.telegram_gateway import normalize_phone

logger = logging.getLogger("telegram_bot")

PHONE_TOKEN_TTL_MINUTES = 30  # same as /auth/phone/verify tokens

SITE = "edutest.narxoz.kz"
TEXT_ASK_CONTACT = (
    "Нажмите кнопку ниже, чтобы подтвердить номер телефона для EduTest.\n\n"
    "Төмендегі батырманы басып, EduTest үшін телефон нөміріңізді растаңыз."
)
BUTTON_SHARE = "📱 Отправить номер / Нөмірді жіберу"
TEXT_DONE = (
    "✅ Номер {phone} подтверждён. Вернитесь на сайт — регистрация продолжится автоматически.\n\n"
    "✅ {phone} нөмірі расталды. Сайтқа оралыңыз — тіркелу автоматты түрде жалғасады."
)
TEXT_LINK_EXPIRED = (
    f"Ссылка устарела. Откройте бота заново через кнопку на сайте {SITE}.\n\n"
    f"Сілтеменің мерзімі өтті. Ботты {SITE} сайтындағы батырма арқылы қайта ашыңыз."
)
TEXT_HELP = (
    f"Это бот для подтверждения номера на {SITE}. Откройте его через кнопку «Подтвердить через Telegram» на сайте.\n\n"
    f"Бұл {SITE} сайтында нөмірді растауға арналған бот. Оны сайттағы «Telegram арқылы растау» батырмасы арқылы ашыңыз."
)
TEXT_NOT_OWN = (
    "Отправьте, пожалуйста, свой номер кнопкой ниже, а не чужой контакт.\n\n"
    "Өзгенің контактісін емес, өз нөміріңізді төмендегі батырма арқылы жіберіңіз."
)
TEXT_ALREADY_REGISTERED = (
    f"Номер {{phone}} уже зарегистрирован. Войдите на {SITE} с этим номером и паролем.\n\n"
    f"{{phone}} нөмірі тіркелген. {SITE} сайтына осы нөмір мен құпия сөз арқылы кіріңіз."
)
TEXT_NOT_REGISTERED = (
    f"Аккаунт с номером {{phone}} не найден. Зарегистрируйтесь на {SITE}.\n\n"
    f"{{phone}} нөмірімен аккаунт табылмады. {SITE} сайтында тіркеліңіз."
)
TEXT_BAD_PHONE = "Не удалось распознать номер. / Нөмірді тану мүмкін болмады."

REMOVE_KEYBOARD = {"remove_keyboard": True}


class BotApiError(Exception):
    pass


def _api(method: str, params: dict, *, timeout: int = 20):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.telegram_bot_token.strip()}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise BotApiError(f"{method}: HTTP {e.code} {e.read()[:200]!r}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise BotApiError(f"{method}: {e}") from e
    if not data.get("ok"):
        raise BotApiError(f"{method}: {data.get('description')}")
    return data["result"]


def _send(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = reply_markup
    try:
        _api("sendMessage", params)
    except BotApiError as e:
        logger.warning("sendMessage to %s failed: %s", chat_id, e)


def _handle_start(db, chat_id: int, user_id: int, payload: str) -> None:
    s = (
        db.query(TelegramBotSession).filter(TelegramBotSession.start_nonce == payload).first()
        if payload
        else None
    )
    if not payload:
        _send(chat_id, TEXT_HELP)
        return
    if not s or s.completed_at or s.error or datetime.utcnow() > s.expires_at:
        _send(chat_id, TEXT_LINK_EXPIRED, REMOVE_KEYBOARD)
        return
    s.telegram_user_id = user_id
    db.commit()
    _send(
        chat_id,
        TEXT_ASK_CONTACT,
        {
            "keyboard": [[{"text": BUTTON_SHARE, "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        },
    )


def _handle_contact(db, chat_id: int, user_id: int, contact: dict) -> None:
    # Telegram fills contact.user_id only for real Telegram accounts; it must be the sender's own account.
    if contact.get("user_id") != user_id:
        _send(chat_id, TEXT_NOT_OWN)
        return
    now = datetime.utcnow()
    s = (
        db.query(TelegramBotSession)
        .filter(
            TelegramBotSession.telegram_user_id == user_id,
            TelegramBotSession.completed_at.is_(None),
            TelegramBotSession.error.is_(None),
            TelegramBotSession.expires_at > now,
        )
        .order_by(TelegramBotSession.created_at.desc())
        .first()
    )
    if not s:
        _send(chat_id, TEXT_LINK_EXPIRED, REMOVE_KEYBOARD)
        return
    phone = normalize_phone(str(contact.get("phone_number") or ""))
    if not phone:
        _send(chat_id, TEXT_BAD_PHONE, REMOVE_KEYBOARD)
        return

    s.phone = phone
    registered = db.query(User).filter(User.phone == phone).first() is not None
    if s.purpose == "register" and registered:
        s.error = "phone_registered"
        db.commit()
        _send(chat_id, TEXT_ALREADY_REGISTERED.format(phone=phone), REMOVE_KEYBOARD)
        return
    if s.purpose == "reset" and not registered:
        s.error = "phone_not_registered"
        db.commit()
        _send(chat_id, TEXT_NOT_REGISTERED.format(phone=phone), REMOVE_KEYBOARD)
        return

    raw = secrets.token_urlsafe(32)
    db.add(
        PhoneVerification(
            phone=phone,
            purpose=s.purpose,
            request_id=f"tg-bot:{user_id}",
            client_ip=s.client_ip,
            expires_at=now,
            verified_at=now,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            token_expires_at=now + timedelta(minutes=PHONE_TOKEN_TTL_MINUTES),
        )
    )
    s.pending_token = raw
    s.completed_at = now
    db.commit()
    _send(chat_id, TEXT_DONE.format(phone=phone), REMOVE_KEYBOARD)


def handle_update(db, update: dict) -> None:
    msg = update.get("message")
    if not msg or (msg.get("chat") or {}).get("type") != "private":
        return
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    if msg.get("contact"):
        _handle_contact(db, chat_id, user_id, msg["contact"])
        return
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        _handle_start(db, chat_id, user_id, parts[1].strip() if len(parts) > 1 else "")
        return
    _send(chat_id, TEXT_HELP)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (settings.telegram_bot_token or "").strip():
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    Base.metadata.create_all(bind=engine)  # telegram_bot_sessions may not exist yet if the API hasn't restarted
    me = _api("getMe", {})
    logger.info("Polling as @%s", me.get("username"))
    offset = None
    while True:
        try:
            updates = _api(
                "getUpdates",
                {"timeout": 30, "offset": offset, "allowed_updates": ["message"]},
                timeout=40,
            )
        except BotApiError as e:
            logger.warning("getUpdates failed: %s", e)
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            with SessionLocal() as db:
                try:
                    handle_update(db, update)
                except Exception:
                    logger.exception("Failed to handle update %s", update.get("update_id"))


if __name__ == "__main__":
    main()
