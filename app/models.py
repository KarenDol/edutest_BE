import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, JSON
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
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subject = relationship("Subject", foreign_keys=[subject_id])


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
