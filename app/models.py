import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    # E.164; students register with a Telegram-verified phone instead of email.
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    patronymic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    iin: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    school_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    school_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ent_profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    signup_approved: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verify_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_verify_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PhoneVerification(Base):
    """One Telegram Gateway code request (sign-up or password reset) and its outcome."""

    __tablename__ = "phone_verifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # register | reset
    request_id: Mapped[str] = mapped_column(String(64))
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # sha256 of the one-time token handed to the client after the code is confirmed.
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    subject_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("subjects.id"), nullable=True)
    question_text: Mapped[str] = mapped_column(Text)
    question_type: Mapped[str] = mapped_column(String(32))
    options: Mapped[list] = mapped_column(JSON, default=list)
    correct_answers: Mapped[list] = mapped_column(JSON, default=list)
    points: Mapped[int] = mapped_column(Integer, default=1)
    shuffle_answer_options: Mapped[bool] = mapped_column(Boolean, default=False)
    show_calculator: Mapped[bool] = mapped_column(Boolean, default=False)
    show_periodic_table: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(16), default="russian")
    difficulty: Mapped[str] = mapped_column(String(1), default="B")
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subject = relationship("Subject", foreign_keys=[subject_id])
    math_content = relationship(
        "QuestionMath",
        back_populates="question",
        uselist=False,
        cascade="all, delete-orphan",
    )


class QuestionMath(Base):
    """LaTeX (MathLive) stem/options and figure URLs; canonical math payload separate from questions row."""

    __tablename__ = "question_math"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    question_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("questions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    stem_latex: Mapped[str] = mapped_column(Text)
    options_latex: Mapped[list] = mapped_column(JSON, default=list)
    image_urls: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    question = relationship("Question", back_populates="math_content")


class Test(Base):
    __tablename__ = "tests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("subjects.id"), nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    shuffle_answer_options: Mapped[bool] = mapped_column(Boolean, default=False)
    show_calculator: Mapped[bool] = mapped_column(Boolean, default=False)
    show_periodic_table: Mapped[bool] = mapped_column(Boolean, default=False)
    # 0 = off, 1 = camera+screen, 3 = camera+screen+face AI (olymp-app proctoringVersion)
    proctoring_version: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subject = relationship("Subject", foreign_keys=[subject_id])
    sections = relationship("TestSection", back_populates="test", order_by="TestSection.section_order")
    test_questions = relationship("TestQuestion", back_populates="test", order_by="TestQuestion.question_order")


class TestSection(Base):
    __tablename__ = "test_sections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    test_id: Mapped[str] = mapped_column(String(36), ForeignKey("tests.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    section_order: Mapped[int] = mapped_column(Integer, default=0)
    subject_picks: Mapped[list] = mapped_column(JSON, default=list)

    test = relationship("Test", back_populates="sections")
    test_questions = relationship("TestQuestion", back_populates="section")


class TestQuestion(Base):
    __tablename__ = "test_questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    test_id: Mapped[str] = mapped_column(String(36), ForeignKey("tests.id", ondelete="CASCADE"))
    section_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("test_sections.id", ondelete="SET NULL"), nullable=True
    )
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("questions.id", ondelete="CASCADE"))
    question_order: Mapped[int] = mapped_column(Integer, default=0)

    test = relationship("Test", back_populates="test_questions")
    section = relationship("TestSection", back_populates="test_questions")
    question = relationship("Question", foreign_keys=[question_id])


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    test_id: Mapped[str] = mapped_column(String(36), ForeignKey("tests.id", ondelete="CASCADE"))
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    total_score: Mapped[float] = mapped_column(Float, default=0)
    max_score: Mapped[float] = mapped_column(Float, default=0)
    percentage: Mapped[float] = mapped_column(Float, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    student = relationship("User", foreign_keys=[student_id])
    test = relationship("Test", foreign_keys=[test_id])


class QuestionReport(Base):
    """Student-reported issue with a question while reviewing a submitted test."""

    __tablename__ = "question_reports"
    __table_args__ = (
        UniqueConstraint("submission_id", "question_id", name="uq_question_report_per_submission"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    submission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    reporter_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    submission = relationship("Submission", foreign_keys=[submission_id])
    question = relationship("Question", foreign_keys=[question_id])
    reporter = relationship("User", foreign_keys=[reporter_id])


class ProctoringReport(Base):
    """Integrity event during a proctored attempt (tab blur, permission loss, …)."""

    __tablename__ = "proctoring_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    submission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    test_id: Mapped[str] = mapped_column(String(36), ForeignKey("tests.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(64))
    timecode: Mapped[str] = mapped_column(String(32), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProctoringDetectionLog(Base):
    """Per-minute face-count string from the in-browser detector (0/1/2+ per second)."""

    __tablename__ = "proctoring_detection_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    submission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    test_id: Mapped[str] = mapped_column(String(36), ForeignKey("tests.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255), default="")
    start_timecode: Mapped[str] = mapped_column(String(32), default="")
    logs: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProctoringChunk(Base):
    """Uploaded webcam/screen recording chunk (webm)."""

    __tablename__ = "proctoring_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    submission_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    test_id: Mapped[str] = mapped_column(String(36), ForeignKey("tests.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32))
    file_name: Mapped[str] = mapped_column(String(255), index=True)
    url: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
