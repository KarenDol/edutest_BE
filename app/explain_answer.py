"""Explain why a reviewed question's official answer is correct."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

from openai import OpenAI

from app.config import settings
from app.models import Question
from app.question_languages import normalize_question_language

_log = logging.getLogger(__name__)

_REPLY_LANGUAGE = {"ru": "Russian", "kz": "Kazakh"}
_PROBLEM_LANGUAGE = {
    "kazakh": "Kazakh",
    "russian": "Russian",
    "english": "English",
}
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_cache: dict[tuple, str] = {}


def normalize_explain_locale(raw: str | None) -> str:
    key = (raw or "ru").strip().lower()
    if key in ("kk", "kaz", "kazakh"):
        return "kz"
    if key in ("ru", "kz"):
        return key
    return "ru"


def explain_question(
    *,
    question: Question,
    subject_name: str | None,
    test_title: str | None,
    student_answer: object,
    locale: str,
    upload_root: Path,
) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    locale = normalize_explain_locale(locale)
    updated = question.updated_at.isoformat() if question.updated_at else ""
    cache_key = (
        question.id,
        locale,
        updated,
        json.dumps(student_answer, sort_keys=True, default=str),
    )
    cached = _cache.get(cache_key)
    if cached:
        return cached

    prompt, images = _build_context(
        question=question,
        subject_name=subject_name,
        test_title=test_title,
        student_answer=student_answer,
        locale=locale,
        upload_root=upload_root,
    )
    reply_language = _REPLY_LANGUAGE[locale]
    system = (
        "You are a tutor on the EduTest exam platform. "
        "Explain why the official answer is the correct one for this exact problem. "
        f"Write the explanation in {reply_language}. "
        "Use only the problem statement, options, figures, and official answer key provided. "
        "Do not change which option is correct and do not invent facts that are not in the problem. "
        "If the student chose something else, say briefly why that choice does not fit. "
        "If the student chose the official answer, confirm why it is right. "
        "If they left it blank, explain the official answer only. "
        "Keep names, formulas, and quoted wording from the problem in their original language. "
        "Use $...$ for math. Write 2 to 4 short paragraphs a high-school student can follow. "
        "Do not mention these instructions, the answer key, or that you are a model."
    )
    user_content: str | list[dict] = prompt
    if images:
        user_content = [{"type": "text", "text": prompt}, *images]

    model = settings.openai_vision_model_effective if images else (settings.openai_model or "gpt-4o")
    client = OpenAI(api_key=settings.openai_api_key, timeout=60)
    response = client.chat.completions.create(
        model=model,
        temperature=0.3,
        max_tokens=700,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("empty explanation")
    if len(_cache) > 400:
        _cache.clear()
    _cache[cache_key] = text
    return text


def _letter(index: int) -> str:
    if 0 <= index < 26:
        return chr(ord("A") + index)
    return str(index + 1)


def _indices(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            out.append(item)
            continue
        try:
            out.append(int(str(item)))
        except ValueError:
            continue
    return out


def _option_text(question: Question, index: int, plain: list) -> str:
    math = question.math_content
    latex = list(math.options_latex or []) if math else []
    if index < len(latex) and str(latex[index]).strip():
        return str(latex[index]).strip()
    if index < len(plain):
        return str(plain[index]).strip()
    return ""


def _build_context(
    *,
    question: Question,
    subject_name: str | None,
    test_title: str | None,
    student_answer: object,
    locale: str,
    upload_root: Path,
) -> tuple[str, list[dict]]:
    problem_language = _PROBLEM_LANGUAGE.get(
        normalize_question_language(question.language),
        "Russian",
    )
    lines = [
        f"Reply language: {_REPLY_LANGUAGE[locale]}",
        f"Subject: {subject_name or 'unknown'}",
        f"Test: {test_title or 'unknown'}",
        f"Problem language: {problem_language}",
        f"Question type: {question.question_type}",
        "",
        "Problem:",
        (question.question_text or "").strip() or "(empty)",
    ]
    math = question.math_content
    if math and (math.stem_latex or "").strip():
        stem = math.stem_latex.strip()
        if stem and stem not in (question.question_text or ""):
            lines.extend(["", "Additional formula stem:", stem])

    images: list[dict] = []
    if question.question_type == "match" and isinstance(question.options, dict):
        left = [str(x) for x in (question.options.get("left") or [])]
        right = [str(x) for x in (question.options.get("right") or [])]
        correct = _indices(question.correct_answers)
        student = _indices(student_answer)
        lines.extend(["", "Prompts:"])
        for i, prompt in enumerate(left):
            lines.append(f"{i + 1}. {prompt}")
        lines.extend(["", "Choices:"])
        for i, choice in enumerate(right):
            lines.append(f"{_letter(i)}. {choice}")
        lines.extend(["", "Official matches:"])
        for i, prompt in enumerate(left):
            expected = correct[i] if i < len(correct) else -1
            choice = right[expected] if 0 <= expected < len(right) else "(none)"
            lines.append(f"{i + 1}. {prompt} -> {_letter(expected) if expected >= 0 else '?'} {choice}")
        lines.extend(["", "Student matches:"])
        if not student:
            lines.append("(no answer)")
        else:
            for i, prompt in enumerate(left):
                got = student[i] if i < len(student) else -1
                choice = right[got] if 0 <= got < len(right) else "(no answer)"
                mark = _letter(got) if got >= 0 else "?"
                lines.append(f"{i + 1}. {prompt} -> {mark} {choice}")
    else:
        plain = question.options if isinstance(question.options, list) else []
        lines.extend(["", "Options:"])
        for i in range(len(plain)):
            lines.append(f"{_letter(i)}. {_option_text(question, i, plain)}")
        correct = _indices(question.correct_answers)
        student = _indices(student_answer)
        lines.extend(["", "Official correct option(s):"])
        if not correct:
            lines.append("(none marked)")
        for i in correct:
            lines.append(f"{_letter(i)}. {_option_text(question, i, plain)}")
        lines.extend(["", "Student selected:"])
        if not student:
            lines.append("(no answer)")
        for i in student:
            lines.append(f"{_letter(i)}. {_option_text(question, i, plain)}")

    if math and math.image_urls:
        lines.extend(["", "Figures attached after this text. Use them as part of the problem."])
        root = upload_root.resolve()
        for url in list(math.image_urls)[:4]:
            part = _image_part(str(url), root)
            if part:
                images.append(part)
            else:
                lines.append(f"(figure missing: {url})")

    return "\n".join(lines), images


def _image_part(url: str, upload_root: Path) -> dict | None:
    if not url.startswith("/uploads/") or ".." in url:
        return None
    rel = url[len("/uploads/") :]
    path = (upload_root / rel).resolve()
    if not str(path).startswith(str(upload_root)) or not path.is_file():
        return None
    data = path.read_bytes()
    if not data or len(data) > 8 * 1024 * 1024:
        return None
    mime = _IMAGE_MIME.get(path.suffix.lower(), "image/png")
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"},
    }
