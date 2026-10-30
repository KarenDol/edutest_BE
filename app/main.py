import hashlib
import logging
import random
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from urllib.parse import quote

import asyncio
import json
import os
import tempfile

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import Base, engine, get_db
from app.db_migrate import (
    migrate_question_reports,
    migrate_questions,
    migrate_test_sections,
    migrate_tests,
    migrate_users,
    migrate_proctoring,
)
from app.explain_answer import explain_question, normalize_explain_locale
from app.question_difficulty import QUESTION_DIFFICULTY_DEFAULT, normalize_question_difficulty
from app.text_breaks import restore_literal_newlines
from app.question_languages import QUESTION_LANGUAGE_DEFAULT, normalize_question_language
from app.deps import get_current_user, require_admin
from app.email_smtp import (
    _smtp_configured,
    log_email_startup_hints,
    notify_admins_new_student,
    notify_student_email_verification,
)
from app import phone_otp, telegram_gateway
from app.models import (
    ProctoringChunk,
    ProctoringDetectionLog,
    ProctoringReport,
    Question,
    QuestionMath,
    QuestionReport,
    PhoneVerification,
    Subject,
    TelegramBotSession,
    Submission,
    Test,
    TestQuestion,
    TestSection,
    User,
)
from app.pdf_import.service import stream_parsed_questions
from app.security import (
    ACCESS_TOKEN_EXPIRE_DAYS,
    ACCESS_TOKEN_REMEMBER_DAYS,
    create_access_token,
    hash_password,
    verify_password,
)

_log = logging.getLogger(__name__)

UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads"
_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_ALLOWED_IMAGE_CT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_users(engine)
    migrate_questions(engine)
    migrate_tests(engine)
    migrate_test_sections(engine)
    migrate_question_reports(engine)
    migrate_proctoring(engine)
    log_email_startup_hints(settings)
    _log.info(
        "Student sign-up verifies the phone number with a code (CASCADE_API_KEY or TELEGRAM_GATEWAY_TOKEN). "
        "(No administrator approval.)"
    )
    with Session(engine) as db:
        if db.query(User).count() == 0 and settings.seed_admin_email and settings.seed_admin_password:
            display = "Admin"
            u = User(
                email=settings.seed_admin_email,
                password_hash=hash_password(settings.seed_admin_password),
                name=display,
                last_name="-",
                first_name="Admin",
                patronymic=None,
                iin="000000000000",
                school_class="-",
                school_name="-",
                ent_profile="FIZMAT",
                role="admin",
                email_verified=True,
            )
            db.add(u)
            db.commit()
    yield


app = FastAPI(title="EduTest by Narxoz API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# StaticFiles requires the directory to exist at import time (before lifespan runs).
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_ROOT)), name="uploads")

from app.proctoring import router as proctoring_router  # noqa: E402

app.include_router(proctoring_router)

from app.whatsapp_webhook import router as whatsapp_router  # noqa: E402

app.include_router(whatsapp_router)


# --- Auth ---

PHONE_CODE_TTL_SECONDS = 300
PHONE_CODE_RESEND_SECONDS = 60
PHONE_CODE_MAX_PER_PHONE_HOUR = 5
PHONE_CODE_MAX_PER_IP_HOUR = 20
PHONE_CODE_MAX_ATTEMPTS = 5
PHONE_TOKEN_TTL_MINUTES = 30


class PhoneCodeSendBody(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    purpose: Literal["register", "reset"]


class PhoneCodeSendResponse(BaseModel):
    verification_id: str
    phone: str
    expires_in: int
    resend_in: int


class PhoneCodeVerifyBody(BaseModel):
    verification_id: str = Field(min_length=1, max_length=36)
    code: str = Field(pattern=r"^\d{4,8}$")


class PhoneCodeVerifyResponse(BaseModel):
    verification_token: str
    phone: str


@app.post("/auth/phone/send-code", response_model=PhoneCodeSendResponse)
def send_phone_code(body: PhoneCodeSendBody, request: Request, db: Session = Depends(get_db)):
    """Send a verification code (Cascade or Telegram Gateway) for sign-up or password reset."""
    if not phone_otp.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Phone verification is not configured (set CASCADE_API_KEY or TELEGRAM_GATEWAY_TOKEN on the API).",
        )
    phone = telegram_gateway.normalize_phone(body.phone)
    if not phone:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    registered = db.query(User).filter(User.phone == phone).first() is not None
    if body.purpose == "register" and registered:
        raise HTTPException(status_code=400, detail="Phone already registered")
    if body.purpose == "reset" and not registered:
        raise HTTPException(status_code=404, detail="No account with this phone number")

    now = datetime.utcnow()
    hour_ago = now - timedelta(hours=1)
    last = (
        db.query(PhoneVerification)
        .filter(PhoneVerification.phone == phone)
        .order_by(PhoneVerification.created_at.desc())
        .first()
    )
    if last and (now - last.created_at).total_seconds() < PHONE_CODE_RESEND_SECONDS:
        wait = PHONE_CODE_RESEND_SECONDS - int((now - last.created_at).total_seconds())
        raise HTTPException(status_code=429, detail=f"Please wait {wait} seconds before requesting a new code.")
    sent_to_phone = (
        db.query(PhoneVerification)
        .filter(PhoneVerification.phone == phone, PhoneVerification.created_at > hour_ago)
        .count()
    )
    client_ip = request.client.host if request.client else None
    sent_from_ip = (
        db.query(PhoneVerification)
        .filter(PhoneVerification.client_ip == client_ip, PhoneVerification.created_at > hour_ago)
        .count()
        if client_ip
        else 0
    )
    if sent_to_phone >= PHONE_CODE_MAX_PER_PHONE_HOUR or sent_from_ip >= PHONE_CODE_MAX_PER_IP_HOUR:
        raise HTTPException(status_code=429, detail="Too many codes requested. Try again later.")

    try:
        request_id = phone_otp.send_code(phone, body.purpose, PHONE_CODE_TTL_SECONDS)
    except phone_otp.OtpSendError as e:
        if e.user_message:
            raise HTTPException(status_code=429 if e.rate_limited else 400, detail=e.user_message) from None
        raise HTTPException(
            status_code=502, detail="Phone verification is temporarily unavailable. Try again later."
        ) from None

    v = PhoneVerification(
        phone=phone,
        purpose=body.purpose,
        request_id=request_id,
        client_ip=client_ip,
        expires_at=now + timedelta(seconds=PHONE_CODE_TTL_SECONDS),
    )
    db.add(v)
    db.commit()
    return PhoneCodeSendResponse(
        verification_id=v.id,
        phone=phone,
        expires_in=PHONE_CODE_TTL_SECONDS,
        resend_in=PHONE_CODE_RESEND_SECONDS,
    )


@app.post("/auth/phone/verify", response_model=PhoneCodeVerifyResponse)
def verify_phone_code(body: PhoneCodeVerifyBody, db: Session = Depends(get_db)):
    """Check the code with the provider; on success return a one-time token for /auth/register or /auth/reset-password."""
    v = db.get(PhoneVerification, body.verification_id)
    if not v or v.verified_at is not None:
        raise HTTPException(status_code=400, detail="Invalid or expired code. Request a new one.")
    if datetime.utcnow() > v.expires_at or v.attempts >= PHONE_CODE_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=400, detail="The code has expired or too many attempts were made. Request a new one."
        )
    v.attempts += 1
    db.commit()
    try:
        code_status = phone_otp.check_code(v.request_id, v.phone, v.purpose, body.code)
    except phone_otp.OtpCheckError:
        raise HTTPException(
            status_code=502, detail="Phone verification is temporarily unavailable. Try again later."
        ) from None
    if code_status == "invalid":
        raise HTTPException(status_code=400, detail="Incorrect code")
    if code_status != "valid":
        v.attempts = PHONE_CODE_MAX_ATTEMPTS
        db.commit()
        raise HTTPException(
            status_code=400, detail="The code has expired or too many attempts were made. Request a new one."
        )

    raw = secrets.token_urlsafe(32)
    v.verified_at = datetime.utcnow()
    v.token_hash = hashlib.sha256(raw.encode()).hexdigest()
    v.token_expires_at = v.verified_at + timedelta(minutes=PHONE_TOKEN_TTL_MINUTES)
    db.commit()
    return PhoneCodeVerifyResponse(verification_token=raw, phone=v.phone)


TG_BOT_SESSION_TTL_MINUTES = 10
TG_BOT_MAX_SESSIONS_PER_IP_HOUR = 300  # free, and schools share one IP
_TG_BOT_ERRORS = {
    "phone_registered": "This phone number is already registered. Log in instead.",
    "phone_not_registered": "No account with this phone number",
}


class TelegramBotStartBody(BaseModel):
    purpose: Literal["register", "reset"]


class TelegramBotStartResponse(BaseModel):
    session_id: str
    poll_secret: str
    bot_url: str
    expires_in: int


class TelegramBotPollBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=36)
    poll_secret: str = Field(min_length=16, max_length=128)


@app.post("/auth/telegram/start", response_model=TelegramBotStartResponse)
def start_telegram_bot_verification(body: TelegramBotStartBody, request: Request, db: Session = Depends(get_db)):
    """Start phone verification via @bot: the student opens bot_url and shares their contact there."""
    if not (settings.telegram_bot_token or "").strip():
        raise HTTPException(status_code=503, detail="Telegram bot is not configured (set TELEGRAM_BOT_TOKEN on the API).")
    now = datetime.utcnow()
    client_ip = request.client.host if request.client else None
    if client_ip:
        recent = (
            db.query(TelegramBotSession)
            .filter(TelegramBotSession.client_ip == client_ip, TelegramBotSession.created_at > now - timedelta(hours=1))
            .count()
        )
        if recent >= TG_BOT_MAX_SESSIONS_PER_IP_HOUR:
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    nonce = secrets.token_urlsafe(24)  # Telegram /start payload: [A-Za-z0-9_-], max 64 chars
    poll_secret = secrets.token_urlsafe(32)
    s = TelegramBotSession(
        purpose=body.purpose,
        start_nonce=nonce,
        poll_secret_hash=hashlib.sha256(poll_secret.encode()).hexdigest(),
        client_ip=client_ip,
        expires_at=now + timedelta(minutes=TG_BOT_SESSION_TTL_MINUTES),
    )
    db.add(s)
    db.commit()
    return TelegramBotStartResponse(
        session_id=s.id,
        poll_secret=poll_secret,
        bot_url=f"https://t.me/{settings.telegram_bot_username}?start={nonce}",
        expires_in=TG_BOT_SESSION_TTL_MINUTES * 60,
    )


@app.post("/auth/telegram/poll")
def poll_telegram_bot_verification(body: TelegramBotPollBody, db: Session = Depends(get_db)):
    """status: pending | verified (with a one-time verification_token, returned once) | error | expired."""
    s = db.get(TelegramBotSession, body.session_id)
    if not s or not secrets.compare_digest(
        s.poll_secret_hash, hashlib.sha256(body.poll_secret.encode()).hexdigest()
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    if s.error:
        return {"status": "error", "detail": _TG_BOT_ERRORS.get(s.error, "Verification failed")}
    if s.pending_token:
        token, s.pending_token = s.pending_token, None
        db.commit()
        return {"status": "verified", "verification_token": token, "phone": s.phone}
    if s.completed_at or datetime.utcnow() > s.expires_at:
        return {"status": "expired"}
    return {"status": "pending"}


def _verified_phone(db: Session, verification_token: str, purpose: str) -> PhoneVerification:
    h = hashlib.sha256(verification_token.encode()).hexdigest()
    v = (
        db.query(PhoneVerification)
        .filter(PhoneVerification.token_hash == h, PhoneVerification.purpose == purpose)
        .first()
    )
    if (
        not v
        or v.consumed_at is not None
        or v.token_expires_at is None
        or datetime.utcnow() > v.token_expires_at
    ):
        raise HTTPException(status_code=400, detail="Phone verification expired. Request a new code.")
    return v


class RegisterBody(BaseModel):
    verification_token: str = Field(min_length=16)
    password: str = Field(min_length=6)
    last_name: str = Field(min_length=1, max_length=255)
    first_name: str = Field(min_length=1, max_length=255)
    patronymic: str | None = None
    iin: str = Field(pattern=r"^\d{12}$")
    school_class: str = Field(pattern=r"^(9|10|11|12)$")
    school_name: str = Field(min_length=1, max_length=500)
    ent_profile: str = Field(pattern="^(FIZMAT|CHEMBIO)$")

    @field_validator("patronymic", mode="before")
    @classmethod
    def patronymic_empty(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None


def _display_name(last: str, first: str, patronymic: str | None) -> str:
    parts = [last.strip(), first.strip()]
    if patronymic and patronymic.strip():
        parts.append(patronymic.strip())
    return " ".join(parts)[:500]


class LoginBody(BaseModel):
    # Phone for students; email still works for admins and accounts created before phone sign-up.
    login: str = Field(min_length=1, max_length=255, validation_alias=AliasChoices("login", "phone", "email"))
    password: str
    remember_me: bool = False


class GoogleLoginBody(BaseModel):
    id_token: str = Field(min_length=10)
    remember_me: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    pending_email_verification: bool = False


class ProfileOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str | None
    email: str | None
    phone: str | None
    role: str
    created_at: datetime
    updated_at: datetime


@app.post("/auth/register", response_model=RegisterResponse)
def register(body: RegisterBody, db: Session = Depends(get_db)):
    v = _verified_phone(db, body.verification_token, "register")
    if db.query(User).filter(User.phone == v.phone).first():
        raise HTTPException(status_code=400, detail="Phone already registered")
    if body.iin == "000000000000":
        raise HTTPException(status_code=400, detail="Invalid IIN")
    if db.query(User).filter(User.iin == body.iin).first():
        raise HTTPException(status_code=400, detail="IIN already registered")
    display = _display_name(body.last_name, body.first_name, body.patronymic)

    user = User(
        email=None,
        phone=v.phone,
        password_hash=hash_password(body.password),
        name=display,
        last_name=body.last_name.strip(),
        first_name=body.first_name.strip(),
        patronymic=body.patronymic,
        iin=body.iin,
        school_class=body.school_class.strip(),
        school_name=body.school_name.strip(),
        ent_profile=body.ent_profile,
        role="student",
        signup_approved=True,
        email_verified=True,  # nothing to confirm: the phone was verified via Telegram
    )
    v.consumed_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)

    notify_admins_new_student(
        settings,
        student_name=display,
        student_phone=v.phone,
        user_id=user.id,
    )

    token = create_access_token(user.id, user.email, user.role, user.name)
    return RegisterResponse(access_token=token)


class PasswordResetBody(BaseModel):
    verification_token: str = Field(min_length=16)
    password: str = Field(min_length=6)


@app.post("/auth/reset-password")
def reset_password(body: PasswordResetBody, db: Session = Depends(get_db)):
    v = _verified_phone(db, body.verification_token, "reset")
    user = db.query(User).filter(User.phone == v.phone).first()
    if not user:
        raise HTTPException(status_code=404, detail="No account with this phone number")
    user.password_hash = hash_password(body.password)
    v.consumed_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


def _user_by_login(db: Session, login: str) -> User | None:
    s = login.strip()
    if "@" in s:
        return db.query(User).filter(func.lower(User.email) == s.lower()).first()
    phone = telegram_gateway.normalize_phone(s)
    return db.query(User).filter(User.phone == phone).first() if phone else None


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = _user_by_login(db, body.login)
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid phone number or password")
    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Please confirm your email address. Check your inbox for the verification link, or use resend confirmation on the login page.",
        )
    days = ACCESS_TOKEN_REMEMBER_DAYS if body.remember_me else ACCESS_TOKEN_EXPIRE_DAYS
    token = create_access_token(user.id, user.email, user.role, user.name, expire_days=days)
    return TokenResponse(access_token=token)


@app.post("/auth/google", response_model=TokenResponse)
def login_google(body: GoogleLoginBody, db: Session = Depends(get_db)):
    if not settings.google_oauth_client_id:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured (set GOOGLE_OAUTH_CLIENT_ID on the API).",
        )
    try:
        from google.auth.transport import requests as google_auth_requests
        from google.oauth2 import id_token as google_id_token

        info = google_id_token.verify_oauth2_token(
            body.id_token,
            google_auth_requests.Request(),
            settings.google_oauth_client_id,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google credential") from None
    if not info.get("email_verified"):
        raise HTTPException(status_code=401, detail="Google account email is not verified")
    email_raw = info.get("email")
    if not email_raw or not isinstance(email_raw, str):
        raise HTTPException(status_code=401, detail="Google token missing email")
    email_norm = email_raw.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == email_norm).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail="No account for this Google email. Register on the sign-up page first using the same email.",
        )
    if not user.email_verified:
        user.email_verified = True
        user.email_verify_token_hash = None
        user.email_verify_expires_at = None
        db.add(user)
        db.commit()
        db.refresh(user)
    days = ACCESS_TOKEN_REMEMBER_DAYS if body.remember_me else ACCESS_TOKEN_EXPIRE_DAYS
    token = create_access_token(user.id, user.email, user.role, user.name, expire_days=days)
    return TokenResponse(access_token=token)


class VerifyEmailBody(BaseModel):
    token: str = Field(min_length=16)


@app.post("/auth/verify-email")
def verify_email(body: VerifyEmailBody, db: Session = Depends(get_db)):
    h = hashlib.sha256(body.token.encode()).hexdigest()
    u = db.query(User).filter(User.email_verify_token_hash == h).first()
    if not u:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link.")
    if u.email_verify_expires_at is not None and datetime.utcnow() > u.email_verify_expires_at:
        raise HTTPException(
            status_code=400,
            detail="This verification link has expired. Use resend confirmation on the login page.",
        )
    u.email_verified = True
    u.email_verify_token_hash = None
    u.email_verify_expires_at = None
    db.commit()
    return {"ok": True}


class ResendVerificationBody(BaseModel):
    email: EmailStr
    password: str


@app.post("/auth/resend-verification")
def resend_verification_email(body: ResendVerificationBody, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.role != "student":
        raise HTTPException(status_code=400, detail="Not applicable")
    if user.email_verified:
        return {"ok": True, "already_verified": True}
    if not _smtp_configured(settings):
        raise HTTPException(
            status_code=503,
            detail="Email verification is not available (SMTP is not configured).",
        )
    pub_base = (settings.public_app_base_url or "").strip().rstrip("/")
    if not pub_base:
        raise HTTPException(
            status_code=503,
            detail="Email verification is not available (PUBLIC_APP_BASE_URL is not set).",
        )
    raw = secrets.token_urlsafe(32)
    user.email_verify_token_hash = hashlib.sha256(raw.encode()).hexdigest()
    user.email_verify_expires_at = datetime.utcnow() + timedelta(hours=48)
    db.add(user)
    db.commit()
    link = f"{pub_base}/ru/auth/verify-email?token={quote(raw, safe='')}"
    ok = notify_student_email_verification(
        settings,
        student_email=user.email,
        student_name=user.name or user.email,
        verification_link=link,
    )
    if not ok:
        raise HTTPException(status_code=503, detail="Could not send email. Try again later.")
    return {"ok": True}


@app.get("/auth/me", response_model=ProfileOut)
def me(user: User = Depends(get_current_user)):
    return ProfileOut(
        id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        role=user.role,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


class StudentListItem(BaseModel):
    id: str
    email: str | None
    phone: str | None
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    school_name: str | None = None
    school_class: str | None = None
    ent_profile: str | None = None
    created_at: datetime


@app.get("/users/students", response_model=list[StudentListItem])
def list_students(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = (
        db.query(User)
        .filter(User.role == "student")
        .order_by(User.created_at.desc())
        .all()
    )
    return [
        StudentListItem(
            id=r.id,
            email=r.email,
            phone=r.phone,
            name=r.name,
            first_name=r.first_name,
            last_name=r.last_name,
            school_name=r.school_name,
            school_class=r.school_class,
            ent_profile=r.ent_profile,
            created_at=r.created_at,
        )
        for r in rows
    ]


# --- Subjects ---


class SubjectCreate(BaseModel):
    name: str


class SubjectOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    created_at: datetime


@app.get("/subjects", response_model=list[SubjectOut])
def list_subjects(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    rows = db.query(Subject).order_by(Subject.name).all()
    return rows


@app.post("/subjects", response_model=SubjectOut)
def create_subject(body: SubjectCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    s = Subject(name=body.name.strip())
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@app.patch("/subjects/{subject_id}", response_model=SubjectOut)
def update_subject(subject_id: str, body: SubjectCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    s = db.get(Subject, subject_id)
    if not s:
        raise HTTPException(404, "Not found")
    s.name = body.name.strip()
    db.commit()
    db.refresh(s)
    return s


@app.delete("/subjects/{subject_id}")
def delete_subject(subject_id: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    s = db.get(Subject, subject_id)
    if not s:
        raise HTTPException(404, "Not found")
    db.delete(s)
    db.commit()
    return {"ok": True}


# --- Questions ---


class QuestionMathPayload(BaseModel):
    stem_latex: str
    options_latex: list[str]
    image_urls: list[str] = Field(default_factory=list)


class MatchOptionsPayload(BaseModel):
    left: list[str] = Field(default_factory=list)
    right: list[str] = Field(default_factory=list)


class QuestionCreate(BaseModel):
    question_text: str = ""
    question_type: str
    subject_id: str
    options: list[str] | MatchOptionsPayload = Field(default_factory=list)
    correct_answers: list[int]
    points: int = Field(default=1, ge=1)
    language: str = Field(default=QUESTION_LANGUAGE_DEFAULT)
    difficulty: str = Field(default=QUESTION_DIFFICULTY_DEFAULT)
    math: QuestionMathPayload | None = None

    @field_validator("options", mode="before")
    @classmethod
    def _coerce_options(cls, v: object) -> object:
        if isinstance(v, dict) and ("left" in v or "right" in v):
            return MatchOptionsPayload.model_validate(v)
        return v

    @field_validator("language")
    @classmethod
    def _validate_language(cls, v: str) -> str:
        return normalize_question_language(v)

    @field_validator("difficulty")
    @classmethod
    def _validate_difficulty(cls, v: str) -> str:
        return normalize_question_difficulty(v)


def _math_public(q: Question) -> dict | None:
    m = q.math_content
    if not m:
        return None
    return {
        "stem_latex": m.stem_latex,
        "options_latex": list(m.options_latex or []),
        "image_urls": list(m.image_urls or []),
    }


def _test_tool_flags(t: Test) -> dict:
    return {
        "shuffle_answer_options": bool(t.shuffle_answer_options),
        "show_calculator": bool(t.show_calculator),
        "show_periodic_table": bool(t.show_periodic_table),
        "proctoring_version": int(getattr(t, "proctoring_version", 0) or 0),
    }


def _question_language(q: Question) -> str:
    return normalize_question_language(q.language)


def _question_difficulty(q: Question) -> str:
    return normalize_question_difficulty(getattr(q, "difficulty", None))


def _validate_image_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    if len(urls) > 12:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at most 12 images per question")
    for u in urls:
        s = str(u).strip()
        if not s:
            continue
        if len(s) > 2048:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "image url too long")
        if ".." in s or not s.startswith("/uploads/"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "invalid image url (must be a path starting with /uploads/)",
            )
        out.append(s)
    return out


def _question_max_points(q: Question) -> int:
    """Total points a question can award (match questions use points × section count)."""
    if q.question_type == "match":
        opts = q.options
        n = 0
        if isinstance(opts, dict):
            left = opts.get("left")
            if isinstance(left, list):
                n = len(left)
        if n == 0 and isinstance(q.correct_answers, list):
            n = len(q.correct_answers)
        return int(q.points or 0) * n
    return int(q.points or 0)


def validate_and_normalize_question(
    db: Session, body: QuestionCreate
) -> tuple[str, str, str, list[str] | dict, list[int], int, str, str, dict | None]:
    """Return normalized question row fields and optional math payload."""
    qtype = body.question_type.strip()
    if qtype not in ("single_choice", "multiple_choice", "match"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "question_type must be single_choice, multiple_choice, or match",
        )
    sid = body.subject_id.strip()
    if not sid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subject_id is required")
    if not db.get(Subject, sid):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subject not found")

    if qtype == "match":
        if body.math is not None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "math mode is not supported for match questions",
            )
        text = restore_literal_newlines(body.question_text).strip()
        if not text:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "question_text is required")
        raw_opts = body.options
        if isinstance(raw_opts, MatchOptionsPayload):
            left_raw, right_raw = raw_opts.left, raw_opts.right
        elif isinstance(raw_opts, dict):
            left_raw = raw_opts.get("left") or []
            right_raw = raw_opts.get("right") or []
        else:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "match questions require options.left and options.right",
            )
        left = [restore_literal_newlines(str(o)).strip() for o in left_raw]
        right = [restore_literal_newlines(str(o)).strip() for o in right_raw]
        if len(left) < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one match prompt is required")
        if len(right) < 2:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least two answer choices are required")
        if any(not o for o in left) or any(not o for o in right):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "every match prompt and choice must be non-empty")
        ca = body.correct_answers
        if len(ca) != len(left):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "correct_answers must have one entry per match prompt",
            )
        if any(not isinstance(i, int) or i < 0 or i >= len(right) for i in ca):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "correct_answers must be valid choice indices")
        if len(set(ca)) != len(ca):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "each answer choice can map to at most one prompt",
            )
        language = normalize_question_language(body.language)
        difficulty = normalize_question_difficulty(body.difficulty)
        return (
            text,
            qtype,
            sid,
            {"left": left, "right": right},
            ca,
            body.points,
            language,
            difficulty,
            None,
        )

    math_block: dict | None = None
    if body.math is not None:
        plain = restore_literal_newlines(body.question_text).strip()
        if not plain:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "question_text is required for math questions (paste calculator output inside $...$ as needed)",
            )
        opts = [restore_literal_newlines(str(o)).strip() for o in body.math.options_latex]
        if len(opts) < 2:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least two math options are required")
        if any(not o for o in opts):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "every math option must be non-empty")
        text = plain
        opts_final = opts
        # Stem lives in question_text (inline $...$ / \\(...\\)); stem_latex is not used for new saves.
        math_block = {
            "stem_latex": "",
            "options_latex": opts,
            "image_urls": _validate_image_urls(list(body.math.image_urls or [])),
        }
    else:
        text = restore_literal_newlines(body.question_text).strip()
        if not text:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "question_text is required")
        opts_final = [restore_literal_newlines(str(o)).strip() for o in body.options]
        if len(opts_final) < 2:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least two options are required")
        if any(not o for o in opts_final):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "every option must have non-empty text")

    ca = body.correct_answers
    if not ca:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one correct answer is required")
    if any(not isinstance(i, int) or i < 0 or i >= len(opts_final) for i in ca):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "correct_answers must be valid option indices")
    if qtype == "single_choice":
        if len(ca) != 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "single_choice requires exactly one correct answer",
            )
    elif len(set(ca)) != len(ca):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "multiple_choice correct_answers must not contain duplicate indices",
        )
    language = normalize_question_language(body.language)
    difficulty = normalize_question_difficulty(body.difficulty)
    return (
        text,
        qtype,
        sid,
        opts_final,
        ca,
        body.points,
        language,
        difficulty,
        math_block,
    )


def _persist_question_math(db: Session, question_id: str, math_block: dict | None) -> None:
    row = db.query(QuestionMath).filter(QuestionMath.question_id == question_id).first()
    if math_block:
        if row:
            row.stem_latex = math_block["stem_latex"]
            row.options_latex = math_block["options_latex"]
            row.image_urls = math_block["image_urls"]
        else:
            db.add(
                QuestionMath(
                    question_id=question_id,
                    stem_latex=math_block["stem_latex"],
                    options_latex=math_block["options_latex"],
                    image_urls=math_block["image_urls"],
                )
            )
    elif row:
        db.delete(row)


@app.post("/questions/upload-image")
def upload_question_image(
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
):
    ct = (file.content_type or "").split(";")[0].strip().lower()
    ext = _ALLOWED_IMAGE_CT.get(ct)
    if not ext:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "unsupported image type (use JPEG, PNG, WebP, or GIF)",
        )
    raw = file.file.read()
    if len(raw) > _MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file too large (max 2MB)")
    name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_ROOT / name
    path.write_bytes(raw)
    return {"url": f"/uploads/{name}"}


@app.get("/questions")
def list_questions(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    rows = (
        db.query(Question)
        .options(joinedload(Question.subject), joinedload(Question.math_content))
        .order_by(Question.created_at.desc())
        .all()
    )
    out = []
    for q in rows:
        sub = q.subject
        sub_dict = {"id": sub.id, "name": sub.name} if sub else None
        out.append(
            {
                "id": q.id,
                "subject_id": q.subject_id,
                "question_text": q.question_text,
                "question_type": q.question_type,
                "options": q.options,
                "correct_answers": q.correct_answers,
                "points": q.points,
                "language": _question_language(q),
                "difficulty": _question_difficulty(q),
                "created_by": q.created_by,
                "created_at": q.created_at.isoformat() + "Z",
                "updated_at": q.updated_at.isoformat() + "Z",
                "subjects": sub_dict,
                "math": _math_public(q),
            }
        )
    return out


@app.get("/questions/count-by-subject")
def question_count_by_subject(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = (
        db.query(Question.subject_id, func.count(Question.id))
        .group_by(Question.subject_id)
        .all()
    )
    return {sid or "": cnt for sid, cnt in rows}


@app.post("/questions")
def create_question(body: QuestionCreate, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    text, qtype, sid, opts, ca, pts, lang, difficulty, math_block = validate_and_normalize_question(db, body)
    q = Question(
        question_text=text,
        question_type=qtype,
        subject_id=sid,
        options=opts,
        correct_answers=ca,
        points=pts,
        language=lang,
        difficulty=difficulty,
        created_by=user.id,
    )
    db.add(q)
    db.flush()
    _persist_question_math(db, q.id, math_block)
    db.commit()
    db.refresh(q)
    db.refresh(q, ["subject", "math_content"])
    sub = q.subject
    sub_dict = {"id": sub.id, "name": sub.name} if sub else None
    return {
        "id": q.id,
        "subject_id": q.subject_id,
        "question_text": q.question_text,
        "question_type": q.question_type,
        "options": q.options,
        "correct_answers": q.correct_answers,
        "points": q.points,
        "language": _question_language(q),
        "difficulty": _question_difficulty(q),
        "created_by": q.created_by,
        "created_at": q.created_at.isoformat() + "Z",
        "updated_at": q.updated_at.isoformat() + "Z",
        "subjects": sub_dict,
        "math": _math_public(q),
    }


@app.patch("/questions/{question_id}")
def update_question(question_id: str, body: QuestionCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Not found")
    text, qtype, sid, opts, ca, pts, lang, difficulty, math_block = validate_and_normalize_question(db, body)
    q.question_text = text
    q.question_type = qtype
    q.subject_id = sid
    q.options = opts
    q.correct_answers = ca
    q.points = pts
    q.language = lang
    q.difficulty = difficulty
    _persist_question_math(db, q.id, math_block)
    db.commit()
    return {"ok": True}


@app.delete("/questions/{question_id}")
def delete_question(question_id: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Not found")
    db.delete(q)
    db.commit()
    return {"ok": True}


# --- PDF import ---


class ImportQuestionItem(BaseModel):
    question_text: str = ""
    options: list[str] = Field(default_factory=list)
    correct_answers: list[int] = Field(default_factory=list)
    question_type: str = "single_choice"
    points: int = Field(default=1, ge=1)
    language: str = Field(default=QUESTION_LANGUAGE_DEFAULT)
    difficulty: str = Field(default=QUESTION_DIFFICULTY_DEFAULT)
    use_math: bool = False
    image_urls: list[str] = Field(default_factory=list)

    @field_validator("language")
    @classmethod
    def _validate_import_language(cls, v: str) -> str:
        return normalize_question_language(v)

    @field_validator("difficulty")
    @classmethod
    def _validate_import_difficulty(cls, v: str) -> str:
        return normalize_question_difficulty(v)


def _sanitize_import_options(options: list[str]) -> list[str]:
    return [str(o).strip() for o in options if str(o).strip()]


def _validate_import_item_fields(item: ImportQuestionItem) -> tuple[str, list[str], list[int]]:
    """Return normalized fields or raise HTTPException with a clear message."""
    text = item.question_text.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "question_text is required")
    opts = _sanitize_import_options(item.options)
    if len(opts) < 2:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"at least two non-empty options are required (got {len(opts)})",
        )
    ca = item.correct_answers
    if not ca:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one correct answer is required")
    if any(not isinstance(i, int) or i < 0 or i >= len(opts) for i in ca):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "correct_answers must be valid option indices",
        )
    qtype = item.question_type.strip()
    if qtype == "single_choice" and len(ca) != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "single_choice requires exactly one correct answer",
        )
    return text, opts, ca


def _import_item_uses_math(item: ImportQuestionItem) -> bool:
    if item.use_math:
        return True
    if "$" in item.question_text:
        return True
    return any("$" in o for o in item.options)


def _question_create_from_import(
    item: ImportQuestionItem,
    subject_id: str,
    *,
    question_text: str,
    options: list[str],
    correct_answers: list[int],
) -> QuestionCreate:
    lang = normalize_question_language(item.language)
    difficulty = normalize_question_difficulty(item.difficulty)
    image_urls = list(item.image_urls or [])
    if _import_item_uses_math(item) or image_urls:
        return QuestionCreate(
            question_text=question_text,
            question_type=item.question_type,
            subject_id=subject_id,
            options=options,
            correct_answers=correct_answers,
            points=item.points,
            language=lang,
            difficulty=difficulty,
            math=QuestionMathPayload(
                stem_latex="",
                options_latex=options,
                image_urls=image_urls,
            ),
        )
    return QuestionCreate(
        question_text=question_text,
        question_type=item.question_type,
        subject_id=subject_id,
        options=options,
        correct_answers=correct_answers,
        points=item.points,
        language=lang,
        difficulty=difficulty,
    )


class ImportBatchCreate(BaseModel):
    subject_id: str
    questions: list[ImportQuestionItem] = Field(min_length=1)


@app.post("/questions/import-pdf")
async def import_questions_from_pdf(
    request: Request,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
):
    if not settings.openai_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "PDF import is not configured (set OPENAI_API_KEY)",
        )
    name = (file.filename or "").lower()
    if not name.endswith((".pdf", ".xlsx")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported file type (PDF or XLSX only)")

    suffix = os.path.splitext(name)[1]
    contents = await file.read()
    if len(contents) > 25 * 1024 * 1024:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File too large (max 25MB)")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    disconnected = asyncio.Event()

    async def watch_disconnect() -> None:
        while True:
            if await request.is_disconnected():
                disconnected.set()
                return
            await asyncio.sleep(0.5)

    disconnect_task = asyncio.create_task(watch_disconnect())

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in stream_parsed_questions(tmp_path, disconnected=disconnected):
                yield chunk
        except json.JSONDecodeError as e:
            yield (json.dumps({"type": "error", "message": f"Invalid AI response: {e}"}) + "\n").encode(
                "utf-8"
            )
        except Exception as e:
            _log.exception("PDF import failed")
            yield (json.dumps({"type": "error", "message": str(e)}) + "\n").encode("utf-8")
        finally:
            disconnect_task.cancel()

    return StreamingResponse(body(), media_type="application/jsonl; charset=utf-8")


@app.post("/questions/import-batch")
def import_questions_batch(
    body: ImportBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    created: list[dict] = []
    errors: list[dict] = []

    for i, item in enumerate(body.questions):
        try:
            q_text, q_opts, q_ca = _validate_import_item_fields(item)
            q_body = _question_create_from_import(
                item,
                body.subject_id,
                question_text=q_text,
                options=q_opts,
                correct_answers=q_ca,
            )
            text, qtype, sid, opts, ca, pts, lang, difficulty, math_block = validate_and_normalize_question(db, q_body)
            q = Question(
                question_text=text,
                question_type=qtype,
                subject_id=sid,
                options=opts,
                correct_answers=ca,
                points=pts,
                language=lang,
                difficulty=difficulty,
                created_by=user.id,
            )
            db.add(q)
            db.flush()
            _persist_question_math(db, q.id, math_block)
            created.append({"index": i, "id": q.id})
        except HTTPException as e:
            errors.append({"index": i, "detail": e.detail})
        except Exception as e:
            errors.append({"index": i, "detail": str(e)})

    db.commit()
    return {"created": created, "errors": errors, "created_count": len(created)}


# --- Tests ---


class SubjectPickInput(BaseModel):
    subject_id: str
    question_count: int = Field(ge=1, le=500)


class TestSectionInput(BaseModel):
    name: str
    duration_minutes: int = Field(ge=1, le=600)
    subject_picks: list[SubjectPickInput] = Field(default_factory=list)
    question_ids: list[str] | None = None


class TestCreate(BaseModel):
    title: str
    description: str | None = None
    subject_id: str | None = None
    duration_minutes: int | None = None
    is_active: bool = False
    shuffle_answer_options: bool = False
    show_calculator: bool = False
    show_periodic_table: bool = False
    proctoring_version: int = 0
    question_ids: list[str] | None = None
    sections: list[TestSectionInput] | None = None
    regenerate_questions: bool = False
    test_count: int = Field(default=1, ge=1, le=50)


def _serialize_test_section(sec: TestSection) -> dict:
    return {
        "id": sec.id,
        "test_id": sec.test_id,
        "name": sec.name,
        "duration_minutes": sec.duration_minutes,
        "section_order": sec.section_order,
        "subject_picks": sec.subject_picks or [],
    }


def _pick_random_question_ids(
    db: Session,
    subject_picks: list[SubjectPickInput],
    exclude_ids: set[str],
) -> list[str]:
    picked: list[str] = []
    for pick in subject_picks:
        pool = (
            db.query(Question.id)
            .filter(Question.subject_id == pick.subject_id)
            .all()
        )
        available = [row[0] for row in pool if row[0] not in exclude_ids]
        if len(available) < pick.question_count:
            sub = db.get(Subject, pick.subject_id)
            sub_name = sub.name if sub else pick.subject_id
            raise HTTPException(
                400,
                detail=(
                    f"Not enough questions for subject «{sub_name}»: "
                    f"need {pick.question_count}, available {len(available)}"
                ),
            )
        sample = random.sample(available, pick.question_count)
        for qid in sample:
            exclude_ids.add(qid)
            picked.append(qid)
    return picked


def _subject_picks_key(picks: list) -> tuple:
    return tuple(sorted((p["subject_id"], p["question_count"]) for p in picks))


def _section_config_key(sec_in: TestSectionInput) -> tuple:
    if sec_in.question_ids:
        return ("manual", tuple(sec_in.question_ids))
    return ("random", _subject_picks_key([p.model_dump() for p in sec_in.subject_picks]))


def _resolve_section_question_ids(
    db: Session, sec_in: TestSectionInput, exclude_ids: set[str]
) -> list[str]:
    if sec_in.question_ids:
        if sec_in.subject_picks:
            raise HTTPException(
                400,
                detail="Section cannot combine random subject picks with manual question_ids",
            )
        qids = list(sec_in.question_ids)
        if len(qids) < 1:
            raise HTTPException(400, detail="Each section must include at least one question")
        seen: set[str] = set()
        for qid in qids:
            if qid in seen:
                raise HTTPException(400, detail="Duplicate question in the same section")
            seen.add(qid)
            if qid in exclude_ids:
                raise HTTPException(400, detail="The same question cannot appear in multiple sections")
            if not db.get(Question, qid):
                raise HTTPException(404, detail=f"Question not found: {qid}")
        exclude_ids.update(qids)
        return qids
    if not sec_in.subject_picks:
        raise HTTPException(
            400,
            detail="Each section needs subject_picks (random) or question_ids (manual)",
        )
    return _pick_random_question_ids(db, sec_in.subject_picks, exclude_ids)


def _update_sections_metadata(db: Session, test: Test, sections: list[TestSectionInput]) -> None:
    existing = (
        db.query(TestSection)
        .filter(TestSection.test_id == test.id)
        .order_by(TestSection.section_order)
        .all()
    )
    if len(existing) != len(sections):
        raise HTTPException(
            400,
            detail="Section count changed. Enable «пересобрать вопросы» to apply new structure.",
        )
    total_duration = 0
    for ex, sec_in in zip(existing, sections):
        ex_key: tuple
        if ex.subject_picks:
            ex_key = ("random", _subject_picks_key(ex.subject_picks or []))
        else:
            ex_qids = [
                tq.question_id
                for tq in db.query(TestQuestion)
                .filter(TestQuestion.section_id == ex.id)
                .order_by(TestQuestion.question_order)
                .all()
            ]
            ex_key = ("manual", tuple(ex_qids))
        if ex_key != _section_config_key(sec_in):
            raise HTTPException(
                400,
                detail="Section questions changed. Enable «пересобрать вопросы» to apply.",
            )
        ex.name = sec_in.name.strip()
        ex.duration_minutes = sec_in.duration_minutes
        total_duration += sec_in.duration_minutes
    test.duration_minutes = total_duration


def _required_questions_per_subject(sections: list[TestSectionInput]) -> dict[str, int]:
    req: dict[str, int] = {}
    for sec in sections:
        for pick in sec.subject_picks:
            req[pick.subject_id] = req.get(pick.subject_id, 0) + pick.question_count
    return req


def _validate_batch_questions_available(
    db: Session,
    sections: list[TestSectionInput],
    test_count: int,
) -> None:
    per_test = _required_questions_per_subject(sections)
    for sid, count_per_test in per_test.items():
        needed = count_per_test * test_count
        available = db.query(Question.id).filter(Question.subject_id == sid).count()
        if available < needed:
            sub = db.get(Subject, sid)
            sub_name = sub.name if sub else sid
            raise HTTPException(
                400,
                detail=(
                    f"Not enough unique questions for subject «{sub_name}»: "
                    f"need {needed} for {test_count} tests ({count_per_test} per test), "
                    f"available {available}"
                ),
            )


def _apply_test_sections(
    db: Session,
    test: Test,
    sections: list[TestSectionInput],
    exclude: set[str] | None = None,
) -> None:
    db.query(TestQuestion).filter(TestQuestion.test_id == test.id).delete()
    db.query(TestSection).filter(TestSection.test_id == test.id).delete()
    used = exclude if exclude is not None else set()
    order = 0
    total_duration = 0
    for sec_idx, sec_in in enumerate(sections):
        name = sec_in.name.strip()
        if not name:
            raise HTTPException(400, detail="Section name is required")
        sec = TestSection(
            test_id=test.id,
            name=name,
            duration_minutes=sec_in.duration_minutes,
            section_order=sec_idx,
            subject_picks=[],
        )
        db.add(sec)
        db.flush()
        total_duration += sec_in.duration_minutes
        if sec_in.question_ids:
            sec.subject_picks = []
        else:
            sec.subject_picks = [p.model_dump() for p in sec_in.subject_picks]
        qids = _resolve_section_question_ids(db, sec_in, used)
        for qid in qids:
            db.add(
                TestQuestion(
                    test_id=test.id,
                    section_id=sec.id,
                    question_id=qid,
                    question_order=order,
                )
            )
            order += 1
    test.duration_minutes = total_duration
    test.subject_id = None


def _apply_manual_questions(db: Session, test: Test, question_ids: list[str]) -> None:
    db.query(TestQuestion).filter(TestQuestion.test_id == test.id).delete()
    db.query(TestSection).filter(TestSection.test_id == test.id).delete()
    for i, qid in enumerate(question_ids):
        db.add(TestQuestion(test_id=test.id, question_id=qid, question_order=i))


def _serialize_test(t: Test, db: Session) -> dict:
    tqs = (
        db.query(TestQuestion)
        .filter(TestQuestion.test_id == t.id)
        .order_by(TestQuestion.question_order)
        .options(joinedload(TestQuestion.question).joinedload(Question.math_content))
        .all()
    )
    secs = (
        db.query(TestSection)
        .filter(TestSection.test_id == t.id)
        .order_by(TestSection.section_order)
        .all()
    )
    sub = db.get(Subject, t.subject_id) if t.subject_id else None
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "subject_id": t.subject_id,
        "duration_minutes": t.duration_minutes,
        "is_active": t.is_active,
        **_test_tool_flags(t),
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() + "Z",
        "updated_at": t.updated_at.isoformat() + "Z",
        "subjects": {"id": sub.id, "name": sub.name} if sub else None,
        "test_sections": [_serialize_test_section(s) for s in secs],
        "test_questions": [
            {
                "id": tq.id,
                "section_id": tq.section_id,
                "question_order": tq.question_order,
                "questions": (
                    {
                        "id": tq.question.id,
                        "question_text": tq.question.question_text,
                        "points": tq.question.points,
                        "subject_id": tq.question.subject_id,
                        "language": _question_language(tq.question),
                        "difficulty": _question_difficulty(tq.question),
                        "math": _math_public(tq.question),
                    }
                    if tq.question
                    else None
                ),
            }
            for tq in tqs
        ],
    }


@app.get("/tests")
def list_tests(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    tests = db.query(Test).order_by(Test.created_at.desc()).all()
    return [_serialize_test(t, db) for t in tests]


@app.get("/tests/active")
def list_active_tests(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    tests = db.query(Test).filter(Test.is_active.is_(True)).order_by(Test.created_at.desc()).all()
    out = []
    for t in tests:
        data = _serialize_test(t, db)
        data["test_questions"] = [{"id": x["id"]} for x in data["test_questions"]]
        out.append(data)
    return out


def _serialize_test_question_row(tq: TestQuestion, *, full: bool) -> dict:
    q = tq.question
    base = {
        "id": tq.id,
        "section_id": tq.section_id,
        "question_order": tq.question_order,
    }
    if not q:
        base["questions"] = None
        return base
    qdata: dict = {
        "id": q.id,
        "question_text": q.question_text,
        "points": q.points,
        "subject_id": q.subject_id,
        "language": _question_language(q),
        "difficulty": _question_difficulty(q),
        "math": _math_public(q),
    }
    if full:
        qdata["question_type"] = q.question_type
        qdata["options"] = q.options
        qdata["correct_answers"] = q.correct_answers
    base["questions"] = qdata
    return base


@app.get("/tests/{test_id}")
def get_test(test_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = db.get(Test, test_id)
    if not t:
        raise HTTPException(404, "Not found")
    if not t.is_active and user.role != "admin":
        raise HTTPException(404, "Not found")
    tqs = (
        db.query(TestQuestion)
        .filter(TestQuestion.test_id == t.id)
        .order_by(TestQuestion.question_order)
        .options(joinedload(TestQuestion.question).joinedload(Question.math_content))
        .all()
    )
    secs = (
        db.query(TestSection)
        .filter(TestSection.test_id == t.id)
        .order_by(TestSection.section_order)
        .all()
    )
    sub = db.get(Subject, t.subject_id) if t.subject_id else None
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "subject_id": t.subject_id,
        "duration_minutes": t.duration_minutes,
        "is_active": t.is_active,
        **_test_tool_flags(t),
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() + "Z",
        "updated_at": t.updated_at.isoformat() + "Z",
        "subjects": {"name": sub.name} if sub else None,
        "test_sections": [_serialize_test_section(s) for s in secs],
        "test_questions": [_serialize_test_question_row(tq, full=True) for tq in tqs],
    }


@app.get("/tests/{test_id}/in-progress-submission")
def in_progress_submission(test_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    s = (
        db.query(Submission)
        .filter(
            Submission.test_id == test_id,
            Submission.student_id == user.id,
            Submission.submitted_at.is_(None),
        )
        .first()
    )
    if not s:
        return {"submission": None}
    return {
        "submission": {
            "id": s.id,
            "student_id": s.student_id,
            "test_id": s.test_id,
            "answers": s.answers,
            "total_score": s.total_score,
            "max_score": s.max_score,
            "percentage": s.percentage,
            "started_at": s.started_at.isoformat() + "Z",
            "submitted_at": None,
        }
    }


@app.post("/tests")
def create_test(body: TestCreate, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    _validate_test_body(body)
    if body.test_count > 1 and not body.sections:
        raise HTTPException(400, detail="Batch creation requires section-based test builder")
    if body.test_count > 1 and body.sections:
        _validate_batch_questions_available(db, body.sections, body.test_count)

    base_title = body.title.strip()
    description = (body.description or "").strip() or None

    if body.test_count == 1:
        t = Test(
            title=base_title,
            description=description,
            subject_id=body.subject_id if body.question_ids else None,
            duration_minutes=body.duration_minutes,
            is_active=body.is_active,
            shuffle_answer_options=body.shuffle_answer_options,
            show_calculator=body.show_calculator,
            show_periodic_table=body.show_periodic_table,
            proctoring_version=body.proctoring_version,
            created_by=user.id,
        )
        db.add(t)
        db.flush()
        if body.sections:
            _apply_test_sections(db, t, body.sections)
        else:
            _apply_manual_questions(db, t, body.question_ids or [])
            if body.duration_minutes is None:
                t.duration_minutes = 60
        db.commit()
        db.refresh(t)
        return _serialize_test(t, db)

    shared_exclude: set[str] = set()
    created: list[dict] = []
    for i in range(1, body.test_count + 1):
        t = Test(
            title=f"{base_title} {i}",
            description=description,
            subject_id=None,
            duration_minutes=body.duration_minutes,
            is_active=body.is_active,
            shuffle_answer_options=body.shuffle_answer_options,
            show_calculator=body.show_calculator,
            show_periodic_table=body.show_periodic_table,
            proctoring_version=body.proctoring_version,
            created_by=user.id,
        )
        db.add(t)
        db.flush()
        _apply_test_sections(db, t, body.sections or [], exclude=shared_exclude)
        created.append(_serialize_test(t, db))
    db.commit()
    return {"tests": created, "created_count": len(created)}


class TestUpdate(BaseModel):
    title: str
    description: str | None = None
    subject_id: str | None = None
    duration_minutes: int | None = None
    is_active: bool = False
    shuffle_answer_options: bool = False
    show_calculator: bool = False
    show_periodic_table: bool = False
    proctoring_version: int = 0
    question_ids: list[str] | None = None
    sections: list[TestSectionInput] | None = None
    regenerate_questions: bool = False


def _validate_test_body(body: TestCreate | TestUpdate) -> None:
    if body.proctoring_version not in (0, 1, 3):
        raise HTTPException(400, detail="proctoring_version must be 0 (off), 1 (camera+screen), or 3 (AI)")
    has_sections = bool(body.sections)
    has_manual = bool(body.question_ids)
    if has_sections == has_manual:
        raise HTTPException(
            400,
            detail="Provide either sections (random builder) or question_ids (manual), not both.",
        )


@app.patch("/tests/{test_id}")
def update_test(test_id: str, body: TestUpdate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    _validate_test_body(body)
    t = db.get(Test, test_id)
    if not t:
        raise HTTPException(404, "Not found")
    t.title = body.title.strip()
    t.description = (body.description or "").strip() or None
    t.is_active = body.is_active
    t.shuffle_answer_options = body.shuffle_answer_options
    t.show_calculator = body.show_calculator
    t.show_periodic_table = body.show_periodic_table
    t.proctoring_version = body.proctoring_version
    if body.sections:
        t.subject_id = None
        if body.regenerate_questions or not db.query(TestSection).filter(TestSection.test_id == t.id).first():
            _apply_test_sections(db, t, body.sections)
        else:
            _update_sections_metadata(db, t, body.sections)
    else:
        t.subject_id = body.subject_id
        t.duration_minutes = body.duration_minutes if body.duration_minutes is not None else 60
        _apply_manual_questions(db, t, body.question_ids or [])
    db.commit()
    return _serialize_test(t, db)


@app.post("/tests/{test_id}/toggle-active")
def toggle_active(test_id: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    t = db.get(Test, test_id)
    if not t:
        raise HTTPException(404, "Not found")
    t.is_active = not t.is_active
    db.commit()
    return {"is_active": t.is_active}


@app.delete("/tests/{test_id}")
def delete_test(test_id: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    t = db.get(Test, test_id)
    if not t:
        raise HTTPException(404, "Not found")
    db.delete(t)
    db.commit()
    return {"ok": True}


# --- Submissions ---

QUESTION_REPORT_REASONS = frozenset(
    {
        "no_correct_option",
        "mistake_in_question",
        "wrong_correct_answer",
        "unclear_wording",
        "other",
    }
)


def _serialize_question_report(r: QuestionReport) -> dict:
    return {
        "id": r.id,
        "submission_id": r.submission_id,
        "question_id": r.question_id,
        "reason": r.reason,
        "comment": r.comment,
        "created_at": r.created_at.isoformat() + "Z",
    }


class QuestionReportCreate(BaseModel):
    question_id: str
    reason: str
    comment: str | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        key = v.strip()
        if key not in QUESTION_REPORT_REASONS:
            raise ValueError(
                f"reason must be one of: {', '.join(sorted(QUESTION_REPORT_REASONS))}"
            )
        return key

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        if len(s) > 2000:
            raise ValueError("comment too long (max 2000 characters)")
        return s


class SubmissionCreate(BaseModel):
    test_id: str


class SubmissionPatch(BaseModel):
    answers: dict | None = None
    total_score: float | None = None
    max_score: float | None = None
    percentage: float | None = None
    submitted_at: str | None = None


def _serialize_submission(s: Submission, db: Session, include_test: bool = False) -> dict:
    student = db.get(User, s.student_id)
    base = {
        "id": s.id,
        "student_id": s.student_id,
        "test_id": s.test_id,
        "answers": s.answers,
        "total_score": s.total_score,
        "max_score": s.max_score,
        "percentage": s.percentage,
        "started_at": s.started_at.isoformat() + "Z",
        "submitted_at": s.submitted_at.isoformat() + "Z" if s.submitted_at else None,
        "profiles": {"name": student.name, "email": student.email, "phone": student.phone} if student else None,
    }
    if include_test:
        t = db.get(Test, s.test_id)
        if t:
            sub = db.get(Subject, t.subject_id) if t.subject_id else None
            base["tests"] = {
                "title": t.title,
                "subjects": {"name": sub.name} if sub else None,
            }
        else:
            base["tests"] = None
    return base


def _serialize_submission_student(s: Submission, db: Session) -> dict:
    t = db.get(Test, s.test_id)
    base = {
        "id": s.id,
        "student_id": s.student_id,
        "test_id": s.test_id,
        "answers": s.answers,
        "total_score": s.total_score,
        "max_score": s.max_score,
        "percentage": s.percentage,
        "started_at": s.started_at.isoformat() + "Z",
        "submitted_at": s.submitted_at.isoformat() + "Z" if s.submitted_at else None,
        "tests": (
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "subject_id": t.subject_id,
                "duration_minutes": t.duration_minutes,
                "is_active": t.is_active,
                "subjects": (
                    {"name": (db.get(Subject, t.subject_id)).name}
                    if t.subject_id and db.get(Subject, t.subject_id)
                    else None
                ),
            }
            if t
            else None
        ),
    }
    return base


@app.get("/submissions")
def list_submissions(
    completed_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Submission)
    if user.role != "admin":
        q = q.filter(Submission.student_id == user.id)
    if completed_only:
        q = q.filter(Submission.submitted_at.isnot(None))
    rows = (
        q.order_by(Submission.submitted_at.desc().nulls_last(), Submission.started_at.desc())
        .limit(500)
        .all()
    )
    if user.role == "admin":
        return [_serialize_submission(s, db, include_test=True) for s in rows]
    return [_serialize_submission_student(s, db) for s in rows]


@app.get("/submissions/admin-completed")
def admin_completed_submissions(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = (
        db.query(Submission)
        .filter(Submission.submitted_at.isnot(None))
        .order_by(Submission.submitted_at.desc().nulls_last())
        .limit(500)
        .all()
    )
    out = []
    for s in rows:
        stu = db.get(User, s.student_id)
        t = db.get(Test, s.test_id)
        subj_name = None
        if t and t.subject_id:
            sub = db.get(Subject, t.subject_id)
            subj_name = sub.name if sub else None
        out.append(
            {
                **{k: v for k, v in _serialize_submission(s, db).items() if k != "profiles"},
                "profiles": {"name": stu.name if stu else None, "email": stu.email if stu else None, "phone": stu.phone if stu else None},
                "tests": {"title": t.title if t else None, "subjects": {"name": subj_name} if subj_name else None},
            }
        )
    return out


@app.get("/submissions/{submission_id}")
def get_submission(submission_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.get(Submission, submission_id)
    if not s:
        raise HTTPException(404, "Not found")
    if user.role != "admin" and s.student_id != user.id:
        raise HTTPException(403, "Forbidden")
    t = db.get(Test, s.test_id)
    if not t:
        raise HTTPException(404, "Not found")
    tqs = (
        db.query(TestQuestion)
        .filter(TestQuestion.test_id == t.id)
        .order_by(TestQuestion.question_order)
        .options(joinedload(TestQuestion.question).joinedload(Question.math_content))
        .all()
    )
    student = db.get(User, s.student_id)
    reports = (
        db.query(QuestionReport)
        .filter(QuestionReport.submission_id == s.id)
        .order_by(QuestionReport.created_at.desc())
        .all()
    )
    return {
        "id": s.id,
        "student_id": s.student_id,
        "test_id": s.test_id,
        "answers": s.answers,
        "total_score": s.total_score,
        "max_score": s.max_score,
        "percentage": s.percentage,
        "started_at": s.started_at.isoformat() + "Z",
        "submitted_at": s.submitted_at.isoformat() + "Z" if s.submitted_at else None,
        "profiles": {"name": student.name, "email": student.email, "phone": student.phone} if student else None,
        "question_reports": [_serialize_question_report(r) for r in reports],
        "tests": {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "subject_id": t.subject_id,
            "duration_minutes": t.duration_minutes,
            "is_active": t.is_active,
            **_test_tool_flags(t),
            "created_at": t.created_at.isoformat() + "Z",
            "updated_at": t.updated_at.isoformat() + "Z",
            "test_questions": [
                {
                    "id": tq.id,
                    "question_order": tq.question_order,
                    "section_id": tq.section_id,
                    "questions": (
                        {
                            "id": tq.question.id,
                            "subject_id": tq.question.subject_id,
                            "question_text": tq.question.question_text,
                            "question_type": tq.question.question_type,
                            "options": tq.question.options,
                            "correct_answers": tq.question.correct_answers,
                            "points": tq.question.points,
                            "language": _question_language(tq.question),
                            "difficulty": _question_difficulty(tq.question),
                            "created_by": tq.question.created_by,
                            "created_at": tq.question.created_at.isoformat() + "Z",
                            "updated_at": tq.question.updated_at.isoformat() + "Z",
                            "math": _math_public(tq.question),
                        }
                        if tq.question
                        else None
                    ),
                }
                for tq in tqs
            ],
            "test_sections": [
                _serialize_test_section(sec)
                for sec in sorted(t.sections, key=lambda s: s.section_order)
            ],
        },
    }


class ExplainAnswerBody(BaseModel):
    locale: str = "ru"


@app.post("/submissions/{submission_id}/questions/{question_id}/explain")
def explain_submission_question(
    submission_id: str,
    question_id: str,
    body: ExplainAnswerBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not settings.openai_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Explanation is not configured",
        )
    s = db.get(Submission, submission_id)
    if not s:
        raise HTTPException(404, "Not found")
    if user.role != "admin" and s.student_id != user.id:
        raise HTTPException(403, "Forbidden")
    if not s.submitted_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Submit the test before requesting an explanation")

    tq = (
        db.query(TestQuestion)
        .filter(TestQuestion.test_id == s.test_id, TestQuestion.question_id == question_id)
        .options(joinedload(TestQuestion.question).joinedload(Question.math_content))
        .first()
    )
    if not tq or not tq.question:
        raise HTTPException(404, "Question is not part of this test")

    question = tq.question
    subject = db.get(Subject, question.subject_id) if question.subject_id else None
    test = db.get(Test, s.test_id)
    answers = s.answers if isinstance(s.answers, dict) else {}
    try:
        text = explain_question(
            question=question,
            subject_name=subject.name if subject else None,
            test_title=test.title if test else None,
            student_answer=answers.get(question_id),
            locale=normalize_explain_locale(body.locale),
            upload_root=UPLOAD_ROOT,
        )
    except Exception:
        _log.exception("explain failed submission=%s question=%s", submission_id, question_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not explain this question")
    return {"explanation": text}


@app.post("/submissions")
def create_submission(body: SubmissionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "student":
        raise HTTPException(403, "Students only")
    t = db.get(Test, body.test_id)
    if not t or not t.is_active:
        raise HTTPException(404, "Test not found")
    test_questions = (
        db.query(Question)
        .join(TestQuestion, TestQuestion.question_id == Question.id)
        .filter(TestQuestion.test_id == t.id)
        .all()
    )
    max_score = sum(_question_max_points(q) for q in test_questions)
    s = Submission(student_id=user.id, test_id=t.id, answers={}, max_score=max_score)
    db.add(s)
    db.commit()
    db.refresh(s)
    return {
        "id": s.id,
        "student_id": s.student_id,
        "test_id": s.test_id,
        "answers": s.answers,
        "total_score": s.total_score,
        "max_score": s.max_score,
        "percentage": s.percentage,
        "started_at": s.started_at.isoformat() + "Z",
        "submitted_at": None,
    }


@app.patch("/submissions/{submission_id}")
def patch_submission(
    submission_id: str,
    body: SubmissionPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = db.get(Submission, submission_id)
    if not s:
        raise HTTPException(404, "Not found")
    if s.student_id != user.id:
        raise HTTPException(403, "Forbidden")
    if body.answers is not None:
        s.answers = body.answers
    if body.total_score is not None:
        s.total_score = body.total_score
    if body.max_score is not None:
        s.max_score = body.max_score
    if body.percentage is not None:
        s.percentage = body.percentage
    if body.submitted_at is not None:
        raw = body.submitted_at.replace("Z", "+00:00")
        s.submitted_at = datetime.fromisoformat(raw).replace(tzinfo=None)
    db.commit()
    return {"ok": True}


@app.post("/submissions/{submission_id}/question-reports")
def create_question_report(
    submission_id: str,
    body: QuestionReportCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "student":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Students only")
    if body.reason == "other" and not body.comment:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "comment is required when reason is 'other'",
        )

    s = db.get(Submission, submission_id)
    if not s or s.student_id != user.id:
        raise HTTPException(404, "Not found")
    if not s.submitted_at:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You can report a question only after submitting the test",
        )

    in_test = (
        db.query(TestQuestion.id)
        .filter(
            TestQuestion.test_id == s.test_id,
            TestQuestion.question_id == body.question_id,
        )
        .first()
    )
    if not in_test:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "question is not part of this test")

    existing = (
        db.query(QuestionReport)
        .filter(
            QuestionReport.submission_id == s.id,
            QuestionReport.question_id == body.question_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already reported this question for this attempt",
        )

    row = QuestionReport(
        submission_id=s.id,
        question_id=body.question_id,
        reporter_id=user.id,
        reason=body.reason,
        comment=body.comment,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_question_report(row)


@app.get("/question-reports")
def list_question_reports(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = (
        db.query(QuestionReport)
        .order_by(QuestionReport.created_at.desc())
        .limit(200)
        .all()
    )
    out = []
    for r in rows:
        sub = db.get(Submission, r.submission_id)
        test = db.get(Test, sub.test_id) if sub else None
        reporter = db.get(User, r.reporter_id)
        question = db.get(Question, r.question_id)
        out.append(
            {
                **_serialize_question_report(r),
                "reporter": {
                    "name": reporter.name if reporter else None,
                    "email": reporter.email if reporter else None,
                    "phone": reporter.phone if reporter else None,
                },
                "test": {"id": test.id, "title": test.title} if test else None,
                "submission_id": r.submission_id,
                "question_preview": (question.question_text[:200] if question else None),
            }
        )
    return out


# --- Dashboard aggregates ---


@app.get("/stats/student-dashboard")
def student_dashboard(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "student":
        raise HTTPException(403, "Students only")
    total_tests = db.query(func.count(Test.id)).filter(Test.is_active.is_(True)).scalar() or 0
    completed = (
        db.query(func.count(Submission.id))
        .filter(Submission.student_id == user.id, Submission.submitted_at.isnot(None))
        .scalar()
        or 0
    )
    rows = (
        db.query(Submission)
        .filter(Submission.student_id == user.id, Submission.submitted_at.isnot(None))
        .all()
    )
    avg = sum(r.percentage for r in rows) / len(rows) if rows else 0
    tests = db.query(Test).filter(Test.is_active.is_(True)).order_by(Test.created_at.desc()).limit(3).all()
    subs = (
        db.query(Submission)
        .filter(Submission.student_id == user.id, Submission.submitted_at.isnot(None))
        .order_by(Submission.submitted_at.desc())
        .limit(5)
        .all()
    )
    return {
        "total_tests": total_tests,
        "completed_tests": completed,
        "average_percentage": avg,
        "tests_preview": [_serialize_test(t, db) for t in tests],
        "recent_submissions": [_serialize_submission_student(s, db) for s in subs],
    }


@app.get("/stats/admin-dashboard")
def admin_dashboard(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    subjects_count = db.query(func.count(Subject.id)).scalar() or 0
    questions_count = db.query(func.count(Question.id)).scalar() or 0
    tests_count = db.query(func.count(Test.id)).scalar() or 0
    students_count = db.query(func.count(User.id)).filter(User.role == "student").scalar() or 0
    recent = db.query(Submission).order_by(Submission.submitted_at.desc().nulls_last()).limit(5).all()
    recent_out = []
    for s in recent:
        stu = db.get(User, s.student_id)
        t = db.get(Test, s.test_id)
        recent_out.append(
            {
                "id": s.id,
                "percentage": s.percentage,
                "submitted_at": s.submitted_at.isoformat() + "Z" if s.submitted_at else None,
                "profiles": {"name": stu.name if stu else None, "email": stu.email if stu else None, "phone": stu.phone if stu else None},
                "tests": {"title": t.title if t else None},
            }
        )
    return {
        "subjects_count": subjects_count,
        "questions_count": questions_count,
        "tests_count": tests_count,
        "students_count": students_count,
        "recent_submissions": recent_out,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
