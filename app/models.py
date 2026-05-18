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
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
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
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subject = relationship("Subject", foreign_keys=[subject_id])
    test_questions = relationship("TestQuestion", back_populates="test", order_by="TestQuestion.question_order")


class TestQuestion(Base):
    __tablename__ = "test_questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    test_id: Mapped[str] = mapped_column(String(36), ForeignKey("tests.id", ondelete="CASCADE"))
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("questions.id", ondelete="CASCADE"))
    question_order: Mapped[int] = mapped_column(Integer, default=0)

    test = relationship("Test", back_populates="test_questions")
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
