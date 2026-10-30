"""Proctoring APIs ported from olymp-app (camera/screen chunks, reports, face logs)."""

from __future__ import annotations

from pathlib import Path
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_admin
from app.models import ProctoringChunk, ProctoringDetectionLog, ProctoringReport, Submission, Test, User

router = APIRouter(prefix="/submissions/{submission_id}/proctoring", tags=["proctoring"])

PROCTORING_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads" / "proctoring"
_MAX_CHUNK_BYTES = 8 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
REPORT_TYPES = frozenset(
    {
        "no_face",
        "more_face",
        "lose_focus",
        "camera_permission_revoked",
        "screen_permission_revoked",
        "media_permission",
    }
)
CHUNK_SOURCES = frozenset({"web-camera", "screen"})


def _owned_submission(db: Session, submission_id: str, user: User) -> Submission:
    s = db.get(Submission, submission_id)
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    if user.role != "admin" and s.student_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return s


def _serialize_report(r: ProctoringReport) -> dict:
    return {
        "id": r.id,
        "submission_id": r.submission_id,
        "type": r.type,
        "timecode": r.timecode,
        "filename": r.filename,
        "description": r.description,
        "created_at": r.created_at.isoformat() + "Z",
    }


def _serialize_log(row: ProctoringDetectionLog) -> dict:
    return {
        "id": row.id,
        "submission_id": row.submission_id,
        "filename": row.filename,
        "start_timecode": row.start_timecode,
        "logs": row.logs,
        "created_at": row.created_at.isoformat() + "Z",
    }


def _serialize_chunk(c: ProctoringChunk) -> dict:
    return {
        "id": c.id,
        "submission_id": c.submission_id,
        "source": c.source,
        "file_name": c.file_name,
        "url": c.url,
        "size_bytes": c.size_bytes,
        "is_final": c.is_final,
        "created_at": c.created_at.isoformat() + "Z",
    }


class ReportCreate(BaseModel):
    type: str
    timecode: str = ""
    filename: str = ""
    description: str = ""

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        key = v.strip()
        if key not in REPORT_TYPES:
            raise ValueError(f"type must be one of: {', '.join(sorted(REPORT_TYPES))}")
        return key


class DetectionLogCreate(BaseModel):
    filename: str = ""
    start_timecode: str = ""
    logs: str = Field(default="", max_length=4000)


@router.post("/reports")
def add_report(
    submission_id: str,
    body: ReportCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _owned_submission(db, submission_id, user)
    row = ProctoringReport(
        submission_id=s.id,
        student_id=s.student_id,
        test_id=s.test_id,
        type=body.type,
        timecode=(body.timecode or "")[:32],
        filename=(body.filename or "")[:255],
        description=(body.description or "")[:4000] or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_report(row)


@router.get("/reports")
def list_reports(
    submission_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _owned_submission(db, submission_id, user)
    if user.role != "admin" and s.student_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    rows = (
        db.query(ProctoringReport)
        .filter(ProctoringReport.submission_id == s.id)
        .order_by(ProctoringReport.created_at.asc())
        .all()
    )
    return {"items": [_serialize_report(r) for r in rows]}


@router.post("/detection-logs")
def add_detection_log(
    submission_id: str,
    body: DetectionLogCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _owned_submission(db, submission_id, user)
    row = ProctoringDetectionLog(
        submission_id=s.id,
        student_id=s.student_id,
        test_id=s.test_id,
        filename=(body.filename or "")[:255],
        start_timecode=(body.start_timecode or "")[:32],
        logs=body.logs or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_log(row)


@router.get("/detection-logs")
def list_detection_logs(
    submission_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    s = db.get(Submission, submission_id)
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    rows = (
        db.query(ProctoringDetectionLog)
        .filter(ProctoringDetectionLog.submission_id == s.id)
        .order_by(ProctoringDetectionLog.created_at.asc())
        .all()
    )
    return {"items": [_serialize_log(r) for r in rows]}


@router.post("/chunks")
def upload_chunk(
    submission_id: str,
    source: str = Form(...),
    file_name: str = Form(...),
    is_final: bool = Form(False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    s = _owned_submission(db, submission_id, user)
    if source not in CHUNK_SOURCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "source must be web-camera or screen")
    if not _SAFE_NAME.match(file_name) or not file_name.endswith(".webm"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid file_name")
    raw = file.file.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty chunk")
    if len(raw) > _MAX_CHUNK_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "chunk too large (max 8MB)")

    dest_dir = PROCTORING_ROOT / s.id
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Avoid overwriting another student's file if names collide.
    stored = f"{uuid.uuid4().hex}_{file_name}"
    path = dest_dir / stored
    path.write_bytes(raw)
    url = f"/uploads/proctoring/{s.id}/{stored}"
    row = ProctoringChunk(
        submission_id=s.id,
        student_id=s.student_id,
        test_id=s.test_id,
        source=source,
        file_name=file_name,
        url=url,
        size_bytes=len(raw),
        is_final=bool(is_final),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_chunk(row)


@router.get("/chunks")
def list_chunks(
    submission_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    s = db.get(Submission, submission_id)
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    rows = (
        db.query(ProctoringChunk)
        .filter(ProctoringChunk.submission_id == s.id)
        .order_by(ProctoringChunk.created_at.asc())
        .all()
    )
    return {"items": [_serialize_chunk(c) for c in rows]}


@router.get("/summary")
def proctoring_summary(
    submission_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    s = db.get(Submission, submission_id)
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "submission not found")
    test = db.get(Test, s.test_id)
    reports = (
        db.query(ProctoringReport)
        .filter(ProctoringReport.submission_id == s.id)
        .order_by(ProctoringReport.created_at.asc())
        .all()
    )
    chunks = (
        db.query(ProctoringChunk)
        .filter(ProctoringChunk.submission_id == s.id)
        .order_by(ProctoringChunk.created_at.asc())
        .all()
    )
    logs = (
        db.query(ProctoringDetectionLog)
        .filter(ProctoringDetectionLog.submission_id == s.id)
        .order_by(ProctoringDetectionLog.created_at.asc())
        .all()
    )
    return {
        "proctoring_version": int(getattr(test, "proctoring_version", 0) or 0) if test else 0,
        "reports": [_serialize_report(r) for r in reports],
        "chunks": [_serialize_chunk(c) for c in chunks],
        "detection_logs": [_serialize_log(r) for r in logs],
    }
