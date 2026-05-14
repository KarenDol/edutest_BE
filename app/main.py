import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import Base, engine, get_db
from app.db_migrate import migrate_users
from app.deps import get_current_user, require_admin
from app.email_smtp import (
    _smtp_configured,
    log_email_startup_hints,
    notify_admins_new_student,
    notify_student_email_verification,
)
from app.models import Question, Subject, Submission, Test, TestQuestion, User
from app.security import (
    ACCESS_TOKEN_EXPIRE_DAYS,
    ACCESS_TOKEN_REMEMBER_DAYS,
    create_access_token,
    hash_password,
    verify_password,
)

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_users(engine)
    log_email_startup_hints(settings)
    _log.info(
        "Student sign-up requires working SMTP and PUBLIC_APP_BASE_URL; each new student gets a verification email "
        "and cannot log in until they confirm. (No administrator approval.)"
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


# --- Auth ---


class RegisterBody(BaseModel):
    email: EmailStr
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
    email: EmailStr
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
    role: str
    created_at: datetime
    updated_at: datetime


@app.post("/auth/register", response_model=RegisterResponse)
def register(body: RegisterBody, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    if body.iin == "000000000000":
        raise HTTPException(status_code=400, detail="Invalid IIN")
    if db.query(User).filter(User.iin == body.iin).first():
        raise HTTPException(status_code=400, detail="IIN already registered")
    display = _display_name(body.last_name, body.first_name, body.patronymic)
    smtp_ok = _smtp_configured(settings)
    pub_base = (settings.public_app_base_url or "").strip().rstrip("/")
    if not smtp_ok:
        raise HTTPException(
            status_code=503,
            detail=(
                "Student registration requires email verification. On the API server, set SMTP in the "
                "FastAPI app environment: SMTP_HOST, SMTP_FROM, and usually SMTP_USER / SMTP_PASSWORD "
                "(see test_builder_BE/.env.example)."
            ),
        )
    if not pub_base:
        raise HTTPException(
            status_code=503,
            detail=(
                "Student registration requires email verification. On the API server, set PUBLIC_APP_BASE_URL "
                "in the FastAPI app environment to your frontend URL (no trailing slash), e.g. https://app.example.com "
                "(see test_builder_BE/.env.example)."
            ),
        )

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.utcnow() + timedelta(hours=48)

    user = User(
        email=body.email,
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
        email_verified=False,
        email_verify_token_hash=token_hash,
        email_verify_expires_at=expires_at,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    link = f"{pub_base}/ru/auth/verify-email?token={quote(raw_token, safe='')}"
    sent = notify_student_email_verification(
        settings,
        student_email=body.email,
        student_name=display,
        verification_link=link,
    )
    if not sent:
        db.delete(user)
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Could not send the verification email. Check SMTP settings and try again.",
        )

    notify_admins_new_student(
        settings,
        student_name=display,
        student_email=body.email,
        user_id=user.id,
    )

    return RegisterResponse(access_token=None, pending_email_verification=True)


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
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
        role=user.role,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


class StudentListItem(BaseModel):
    id: str
    email: str
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


class QuestionCreate(BaseModel):
    question_text: str
    question_type: str
    subject_id: str
    options: list[str]
    correct_answers: list[int]
    points: int = Field(default=1, ge=1)


def validate_and_normalize_question(db: Session, body: QuestionCreate) -> tuple[str, str, str, list[str], list[int], int]:
    """Require full question data; return normalized values for persistence."""
    text = body.question_text.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "question_text is required")
    qtype = body.question_type.strip()
    if qtype not in ("single_choice", "multiple_choice"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "question_type must be single_choice or multiple_choice",
        )
    sid = body.subject_id.strip()
    if not sid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subject_id is required")
    if not db.get(Subject, sid):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subject not found")
    opts = [str(o).strip() for o in body.options]
    if len(opts) < 2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least two options are required")
    if any(not o for o in opts):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "every option must have non-empty text")
    ca = body.correct_answers
    if not ca:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one correct answer is required")
    if any(not isinstance(i, int) or i < 0 or i >= len(opts) for i in ca):
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
    return text, qtype, sid, opts, ca, body.points


@app.get("/questions")
def list_questions(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    rows = (
        db.query(Question)
        .options(joinedload(Question.subject))
        .order_by(Question.created_at.desc())
        .all()
    )
    out = []
    for q in rows:
        sub = q.subject
        out.append(
            {
                "id": q.id,
                "subject_id": q.subject_id,
                "question_text": q.question_text,
                "question_type": q.question_type,
                "options": q.options,
                "correct_answers": q.correct_answers,
                "points": q.points,
                "created_by": q.created_by,
                "created_at": q.created_at.isoformat() + "Z",
                "updated_at": q.updated_at.isoformat() + "Z",
                "subjects": {"id": sub.id, "name": sub.name} if sub else None,
            }
        )
    return out


@app.post("/questions")
def create_question(body: QuestionCreate, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    text, qtype, sid, opts, ca, pts = validate_and_normalize_question(db, body)
    q = Question(
        question_text=text,
        question_type=qtype,
        subject_id=sid,
        options=opts,
        correct_answers=ca,
        points=pts,
        created_by=user.id,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    db.refresh(q, ["subject"])
    sub = q.subject
    return {
        "id": q.id,
        "subject_id": q.subject_id,
        "question_text": q.question_text,
        "question_type": q.question_type,
        "options": q.options,
        "correct_answers": q.correct_answers,
        "points": q.points,
        "created_by": q.created_by,
        "created_at": q.created_at.isoformat() + "Z",
        "updated_at": q.updated_at.isoformat() + "Z",
        "subjects": {"id": sub.id, "name": sub.name} if sub else None,
    }


@app.patch("/questions/{question_id}")
def update_question(question_id: str, body: QuestionCreate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(404, "Not found")
    text, qtype, sid, opts, ca, pts = validate_and_normalize_question(db, body)
    q.question_text = text
    q.question_type = qtype
    q.subject_id = sid
    q.options = opts
    q.correct_answers = ca
    q.points = pts
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


# --- Tests ---


class TestCreate(BaseModel):
    title: str
    description: str | None = None
    subject_id: str | None = None
    duration_minutes: int = 60
    is_active: bool = False
    question_ids: list[str]


def _serialize_test(t: Test, db: Session) -> dict:
    tqs = (
        db.query(TestQuestion)
        .filter(TestQuestion.test_id == t.id)
        .order_by(TestQuestion.question_order)
        .options(joinedload(TestQuestion.question))
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
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() + "Z",
        "updated_at": t.updated_at.isoformat() + "Z",
        "subjects": {"id": sub.id, "name": sub.name} if sub else None,
        "test_questions": [
            {
                "id": tq.id,
                "question_order": tq.question_order,
                "questions": (
                    {
                        "id": tq.question.id,
                        "question_text": tq.question.question_text,
                        "points": tq.question.points,
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
        .options(joinedload(TestQuestion.question))
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
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() + "Z",
        "updated_at": t.updated_at.isoformat() + "Z",
        "subjects": {"name": sub.name} if sub else None,
        "test_questions": [
            {
                "id": tq.id,
                "question_order": tq.question_order,
                "questions": (
                    {
                        "id": tq.question.id,
                        "question_text": tq.question.question_text,
                        "question_type": tq.question.question_type,
                        "options": tq.question.options,
                        "correct_answers": tq.question.correct_answers,
                        "points": tq.question.points,
                    }
                    if tq.question
                    else None
                ),
            }
            for tq in tqs
        ],
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
    t = Test(
        title=body.title.strip(),
        description=(body.description or "").strip() or None,
        subject_id=body.subject_id,
        duration_minutes=body.duration_minutes,
        is_active=body.is_active,
        created_by=user.id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    for i, qid in enumerate(body.question_ids):
        db.add(TestQuestion(test_id=t.id, question_id=qid, question_order=i))
    db.commit()
    return _serialize_test(t, db)


class TestUpdate(BaseModel):
    title: str
    description: str | None = None
    subject_id: str | None = None
    duration_minutes: int = 60
    is_active: bool = False
    question_ids: list[str]


@app.patch("/tests/{test_id}")
def update_test(test_id: str, body: TestUpdate, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    t = db.get(Test, test_id)
    if not t:
        raise HTTPException(404, "Not found")
    t.title = body.title.strip()
    t.description = (body.description or "").strip() or None
    t.subject_id = body.subject_id
    t.duration_minutes = body.duration_minutes
    t.is_active = body.is_active
    db.query(TestQuestion).filter(TestQuestion.test_id == t.id).delete()
    for i, qid in enumerate(body.question_ids):
        db.add(TestQuestion(test_id=t.id, question_id=qid, question_order=i))
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
        "profiles": {"name": student.name, "email": student.email} if student else None,
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
                "profiles": {"name": stu.name if stu else None, "email": stu.email if stu else None},
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
        .options(joinedload(TestQuestion.question))
        .all()
    )
    student = db.get(User, s.student_id)
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
        "profiles": {"name": student.name, "email": student.email} if student else None,
        "tests": {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "subject_id": t.subject_id,
            "duration_minutes": t.duration_minutes,
            "is_active": t.is_active,
            "created_at": t.created_at.isoformat() + "Z",
            "updated_at": t.updated_at.isoformat() + "Z",
            "test_questions": [
                {
                    "id": tq.id,
                    "question_order": tq.question_order,
                    "questions": (
                        {
                            "id": tq.question.id,
                            "subject_id": tq.question.subject_id,
                            "question_text": tq.question.question_text,
                            "question_type": tq.question.question_type,
                            "options": tq.question.options,
                            "correct_answers": tq.question.correct_answers,
                            "points": tq.question.points,
                            "created_by": tq.question.created_by,
                            "created_at": tq.question.created_at.isoformat() + "Z",
                            "updated_at": tq.question.updated_at.isoformat() + "Z",
                        }
                        if tq.question
                        else None
                    ),
                }
                for tq in tqs
            ],
        },
    }


@app.post("/submissions")
def create_submission(body: SubmissionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "student":
        raise HTTPException(403, "Students only")
    t = db.get(Test, body.test_id)
    if not t or not t.is_active:
        raise HTTPException(404, "Test not found")
    max_score = (
        db.query(func.coalesce(func.sum(Question.points), 0))
        .join(TestQuestion, TestQuestion.question_id == Question.id)
        .filter(TestQuestion.test_id == t.id)
        .scalar()
    )
    max_score = int(max_score or 0)
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
                "profiles": {"name": stu.name if stu else None, "email": stu.email if stu else None},
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
