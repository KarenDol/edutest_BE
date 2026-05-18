import base64
import json
import re

from openai import OpenAI

from app.config import settings

_LATEX_RULES = """
MATHEMATICS (CRITICAL):
- Every mathematical expression MUST be written in LaTeX wrapped in $...$ (inline).
- Examples: cube root → $\\sqrt[3]{(-5)^3 \\cdot 2^9}$; fractions → $\\frac{1}{2}$; powers → $2^9$.
- Read formulas from the page image carefully; do NOT output broken plain-text like "3 ( - 5 ) 3 • 2 9".
- Answer options that are numbers with signs still use LaTeX when they are math answers, e.g. $-40$, $40$.
- Plain Russian/English words stay outside $...$; only math goes inside delimiters.
- In JSON strings, escape backslashes (write \\\\ for one backslash in LaTeX).

CONTENT FORMAT:
- Use plain text for the problem statement (no HTML tags).
- Combine words and inline math, e.g. "Вычислите: $\\sqrt[3]{(-5)^3 \\cdot 2^9}$"

IGNORE METADATA:
- Skip lines like {Этап}=01, {Правильный ответ}=A, {Учебник}=..., etc.
- Do not include @@@001-style ids or header lines like "1_Математика_рус" in the question text.
"""

_SYSTEM_PROMPT_TEXT = f"""
You are a problem-parsing model.

Your task is to receive raw text from ONE page of a test document, split it into individual multiple-choice questions, and choose the correct answer.

{_LATEX_RULES}

────────────────────
OUTPUT REQUIREMENTS
────────────────────
Your output MUST:
- Be a valid Python list
- Contain ONLY dictionaries (one dictionary per problem)
- Contain NO explanations, comments, markdown fences, or extra text
- Match the schema EXACTLY

Return ONLY the list. Nothing before or after it.

────────────────────
PROBLEM STRUCTURE
────────────────────
Each problem MUST be a dictionary with EXACTLY these keys:

- "content": string — problem statement (plain text + $...$ LaTeX for math)
- "answers": list of EXACTLY 4 dicts, each with:
  - "content": string (plain text + $...$ LaTeX)
  - "isTrue": boolean

Exactly ONE answer must have "isTrue": true.

ANSWERS:
- Do NOT add, remove, reorder, or rewrite answers
- Remove only leading A)/B)/C)/D) labels; keep the answer value
- Do NOT infer missing answers

STRICT:
- One dictionary per problem; exactly 4 options each
- Remove leading problem numbers from the question (e.g. "68.") unless part of the body
- Ignore point values like "(1 point)"
"""

_SYSTEM_PROMPT_VISION = f"""
You are a problem-parsing model.

You receive an image of ONE page from a test document (often Russian/Kazakh math tests). Extract every multiple-choice question visible on this page and identify the correct answer when marked.

{_LATEX_RULES}

────────────────────
OUTPUT REQUIREMENTS
────────────────────
Return ONLY a valid Python list of dictionaries. No markdown, no commentary.

Each dictionary:
- "content": string — problem statement
- "answers": list of EXACTLY 4 {{"content": string, "isTrue": boolean}}

Exactly ONE "isTrue": true per question.

If the page has no questions, return [].

ANSWERS:
- Remove only A)/B)/C)/D) prefixes
- Preserve values exactly as shown, using LaTeX for math
"""


def _client() -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=settings.openai_api_key)


def _parse_list_response(raw: str) -> list:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s.strip())
    return json.loads(s)


def parse_questions_page(text: str) -> str:
    client = _client()
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_TEXT},
            {"role": "user", "content": str(text)},
        ],
    )
    return response.choices[0].message.content.strip()


def parse_questions_page_image(
    png_bytes: bytes,
    page_number: int,
    supplementary_text: str = "",
) -> str:
    """Parse one PDF page from its rendered image (vision)."""
    client = _client()
    b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    user_parts: list[dict] = [
        {
            "type": "text",
            "text": (
                f"This is page {page_number} of the document. "
                "Extract all multiple-choice questions from the image."
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        },
    ]
    if supplementary_text.strip():
        user_parts.insert(
            1,
            {
                "type": "text",
                "text": (
                    "Optional noisy text layer from PDF (use only as a hint; trust the image for math):\n"
                    + supplementary_text.strip()[:8000]
                ),
            },
        )

    model = settings.openai_vision_model_effective
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_VISION},
            {"role": "user", "content": user_parts},
        ],
    )
    return response.choices[0].message.content.strip()


def parse_questions_page_image_to_list(
    png_bytes: bytes,
    page_number: int,
    supplementary_text: str = "",
) -> list:
    raw = parse_questions_page_image(png_bytes, page_number, supplementary_text)
    return _parse_list_response(raw)


def parse_questions_page_to_list(text: str) -> list:
    raw = parse_questions_page(text)
    return _parse_list_response(raw)
