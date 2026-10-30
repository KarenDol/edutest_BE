import ast
import base64
import json
import re

from openai import OpenAI

from app.config import settings
from app.question_difficulty import normalize_question_difficulty
from app.question_languages import normalize_question_language
from app.text_breaks import restore_literal_newlines

_LATEX_RULES = """
MATHEMATICS (CRITICAL — the UI only renders math inside $...$):
- EVERY mathematical expression in the question "content" AND in EVERY answer "content"
  MUST be LaTeX wrapped in inline $...$ delimiters. No exceptions.
- NEVER output bare LaTeX (backslash commands, ^, _, \\frac, \\sqrt, etc.) outside $...$.
- Plain Russian/English/Kazakh words stay outside $...$; only math goes inside delimiters.
- Question stems and answer options follow the SAME wrapping rules.

WRONG (will not render — never output like this):
  "content": "Вычислите: \\sqrt[3]{(-5)^3 \\cdot 2^9}"
  "content": "40"   (when the option is a math answer — still use $...$)

RIGHT:
  "content": "Вычислите: $\\sqrt[3]{(-5)^3 \\cdot 2^9}$"
  answer "content": "$-40$", "$40$", "$\\frac{1}{2}$"

More examples:
  cube root → $\\sqrt[3]{(-5)^3 \\cdot 2^9}$; powers → $2^9$.
- Fractions MUST use \\frac{{numerator}}{{denominator}} inside $...$, e.g. $\\left(\\frac{{1}}{{5}}\\right)^{{-1}}$.
  NEVER write frac{{...}} without the backslash (invalid LaTeX).
- Read formulas from the page image carefully; do NOT output broken plain-text like "3 ( - 5 ) 3 • 2 9".
- Output is a Python list literal (not JSON). LaTeX backslashes in strings are fine, e.g. "$\\sqrt{x}$".

CONTENT FORMAT:
- Use plain text for the problem statement (no HTML tags).
- Combine words and inline math: "Вычислите: $\\sqrt[3]{(-5)^3 \\cdot 2^9}$"
- Put each numbered item (I. II. III. IV., or 1. 2. 3.) on its own line.
- A line break must be a real newline inside the string. Never write the two characters backslash and n in the question or answers.

TABLES:
- When the problem has a table, put that table in "content" as a markdown table. One row per line, with a separator under the header.
- Keep the sentences before and after the table on their own lines.
- Example:
  В таблице представлены данные.
  | Месяц | Саша | Наташа |
  | --- | --- | --- |
  | Январь | 8000 | 7200 |
  | Февраль | 7300 | 8500 |
  Исходя из таблицы, выберите верное утверждение
- Do not flatten a table into a line of words separated by spaces. Do not use HTML.

BEFORE YOU FINISH — self-check every "content" string:
  If it contains \\, ^, _, curly braces used for math, or numeric math answers,
  confirm each expression is fully inside $...$ (opening $ before math, closing $ after).

LANGUAGE (required on every problem):
- Add "language": exactly one of "kazakh", "russian", "english".
- Detect from the wording of the question and answers (Cyrillic Kazakh vs Russian vs English).
- Use document hints when present (e.g. header contains "қаз", "рус", "kaz", "rus", "eng").
- Default to "russian" only when the language is genuinely unclear.

DIFFICULTY (required on every problem):
- Add "difficulty": exactly one of "A", "B", "C".
- Read the question's level from the page: a mark such as A/B/C or А/В/С, "уровень", "деңгей",
  "сложность", or metadata like {Уровень}=B, {Сложность}=A, {Деңгей}=C.
- A heading that starts a block ("Уровень A", "B деңгей") applies to every question in that block
  until the next level heading, even when the letter is not repeated on each question.
- Do NOT use the answer-choice letter (A) B) C) D), or {Правильный ответ}, as difficulty.
  Those mark the correct option, not the level.
- Do not leave the difficulty mark inside "content"; put it only in "difficulty".
- If the page shows no difficulty mark, use "B".
- When a question is cut off, keep its difficulty mark in "unextracted" so the next page still has it.

PICTURES:
- Every problem also has "figures" and "carried_figures", each a list of integers, or [].
- The user message says how many pictures were cropped from this page (top to bottom, ids starting at 0) and how many were carried from the previous page.
- Put a picture id on the question it belongs to: a map, flag, portrait, chart, diagram, or any drawing the question asks about.
- "figures" is for pictures on this page. "carried_figures" is for pictures carried with the unfinished fragment from the previous page.
- Use [] when the question has no picture, and when the user message says there are no pictures.
- Do not invent ids. Do not write [[FIGURE_n]] inside "content" or answers.
- If the unfinished question at the bottom owns a picture on this page, start "unextracted" with [[FIGURE_n]] for each of those ids.

IGNORE METADATA:
- Skip lines like {Этап}=01, {Правильный ответ}=A, {Учебник}=..., etc.
- Do not include @@@001-style ids or header lines like "1_Математика_рус" in the question text.

PAGE BOUNDARIES (a question often starts at the bottom of one page and finishes on the next):
- Put a question in "questions" ONLY when its statement AND all 4 answer options are fully present
  (in this page, plus any text prepended from the previous page).
- If a question is cut off at the end of this page — the stem continues, or fewer than 4 options are visible —
  do NOT include it in "questions". Do NOT invent the missing options.
- Transcribe that unfinished tail into "unextracted": the stem and any options already visible,
  faithfully, with math in $...$. This fragment is prepended to the next page so the question is not lost.
- "unextracted" must contain ONLY that unfinished question. No headers, footers, page numbers, metadata,
  and no question you already placed in "questions".
- If this page ends on a complete question, "unextracted" must be "".
- When text from the previous page is prepended, it is the start of a question that continues here.
  Merge it with the continuation and emit one complete question. Once it is complete, do not copy it into "unextracted".
"""

_SYSTEM_PROMPT_TEXT = f"""
You are a problem-parsing model.

Your task is to receive raw text from ONE page of a test document, split it into individual multiple-choice questions, and choose the correct answer.
Text from the previous page may be prepended when a question was cut off there. Merge that fragment with this page.

{_LATEX_RULES}

────────────────────
OUTPUT REQUIREMENTS
────────────────────
Your output MUST be ONE Python dict with EXACTLY these keys:
- "questions": a list of problem dictionaries (one dictionary per complete problem; [] if none)
- "unextracted": a string — the unfinished question at the end of this page, or ""

The dict must contain NO explanations, comments, markdown fences, or extra text.
Match the schema EXACTLY. Return ONLY the dict. Nothing before or after it.

────────────────────
PROBLEM STRUCTURE
────────────────────
Each problem MUST be a dictionary with EXACTLY these keys:

- "language": string — one of "kazakh", "russian", "english"
- "difficulty": string — one of "A", "B", "C"
- "content": string — problem statement (plain text; ALL math in $...$)
- "answers": list of EXACTLY 4 dicts, each with:
  - "content": string (plain text; ALL math in $...$ — never bare \\sqrt, \\frac, etc.)
  - "isTrue": boolean
- "figures": list of integers — picture ids on this page, or []
- "carried_figures": list of integers — picture ids carried from the previous page, or []

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

You receive an image of ONE page from a test document (often Russian/Kazakh math tests). Extract every complete multiple-choice question visible on this page and identify the correct answer when marked.
Text from the previous page may be prepended when a question was cut off there. Merge that fragment with the continuation on this page.

{_LATEX_RULES}

────────────────────
OUTPUT REQUIREMENTS
────────────────────
Return ONLY one Python dict. No markdown, no commentary.

The dict has EXACTLY these keys:
- "questions": list of complete problems (or [])
- "unextracted": string — unfinished question cut off at the end of this page, or ""

Each problem dictionary:
- "language": string — one of "kazakh", "russian", "english"
- "difficulty": string — one of "A", "B", "C"
- "content": string — problem statement (ALL math in $...$)
- "answers": list of EXACTLY 4 {{"content": string, "isTrue": boolean}} (ALL math in $...$)
- "figures": list of integers — picture ids on this page, or []
- "carried_figures": list of integers — picture ids carried from the previous page, or []

Exactly ONE "isTrue": true per question.

If the page has no complete questions, "questions" is []. Still fill "unextracted" when a question is cut off.

ANSWERS:
- Remove only A)/B)/C)/D) prefixes
- Preserve values exactly as shown, using LaTeX for math
"""


def _client() -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=settings.openai_api_key)


def _double_backslashes_in_quoted_strings(text: str) -> str:
    """Prepare model output for ast.literal_eval without eating LaTeX (\\f in \\frac, \\r in \\right)."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    quote: str | None = None
    raw_string = False

    while i < n:
        if not in_string:
            if i + 1 < n and text[i] in "rR" and text[i + 1] in "\"'":
                raw_string = True
                quote = text[i + 1]
                out.append(text[i : i + 2])
                i += 2
                in_string = True
                continue
            if text[i] in "\"'":
                in_string = True
                quote = text[i]
                raw_string = False
                out.append(text[i])
                i += 1
                continue
            out.append(text[i])
            i += 1
            continue

        ch = text[i]
        if raw_string:
            out.append(ch)
            if ch == quote:
                in_string = False
                quote = None
                raw_string = False
            i += 1
            continue

        if ch == "\\":
            # \\alpha is already escaped. Doubling it again stores \\alpha,
            # which MathLive prints as an error instead of α.
            if i + 1 < n and text[i + 1] == "\\":
                out.append("\\\\")
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        if ch == quote:
            in_string = False
            quote = None
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1

    return "".join(out)


def _literal_eval_lenient(s: str):
    """Parse Python-literal list; accept JSON-style true/false/null booleans."""
    fixed = re.sub(r"\btrue\b", "True", s)
    fixed = re.sub(r"\bfalse\b", "False", fixed)
    fixed = re.sub(r"\bnull\b", "None", fixed)
    fixed = _double_backslashes_in_quoted_strings(fixed)
    return ast.literal_eval(fixed)


_EMPTY_UNEXTRACTED = {"", "none", "null", "n/a", "[]", '""', "''"}


def _parse_model_value(raw: str):
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s.strip())

    last_err: Exception | None = None
    for parser in (json.loads, _literal_eval_lenient):
        try:
            data = parser(s)
            if isinstance(data, (list, dict)):
                return data
        except (json.JSONDecodeError, ValueError, SyntaxError) as e:
            last_err = e
            continue

    raise json.JSONDecodeError(
        f"Could not parse model response: {last_err}",
        s,
        getattr(last_err, "pos", 0) if last_err else 0,
    )


def _clean_unextracted(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(str(part).strip() for part in value if str(part).strip())
    elif not isinstance(value, str):
        value = str(value)
    text = restore_literal_newlines(value).strip()
    if text.lower() in _EMPTY_UNEXTRACTED:
        return ""
    return text[:8000]


def _split_page_payload(raw: str) -> tuple[list[dict], str]:
    """Return complete questions and the unfinished fragment to prepend to the next page."""
    data = _parse_model_value(raw)
    unextracted = ""
    questions = data
    if isinstance(data, dict) and ("questions" in data or "unextracted" in data):
        questions = data.get("questions") or []
        unextracted = _clean_unextracted(data.get("unextracted"))
    elif (
        isinstance(data, list)
        and len(data) == 1
        and isinstance(data[0], dict)
        and "questions" in data[0]
    ):
        questions = data[0].get("questions") or []
        unextracted = _clean_unextracted(data[0].get("unextracted"))

    if not isinstance(questions, list):
        raise json.JSONDecodeError("Model response questions was not a list", raw, 0)
    return _normalize_parsed_items(questions), unextracted


def _prepend_carryover(text: str, carryover: str) -> str:
    fragment = (carryover or "").strip()
    if not fragment:
        return text
    return (
        "UNEXTRACTED FROM THE PREVIOUS PAGE "
        "(prepend this — it is the start of a question cut off at the page break; "
        "merge it with the continuation below into one question):\n"
        f"{fragment}\n\n"
        "CURRENT PAGE:\n"
        f"{text}"
    )


def _normalize_parsed_items(items: list) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        if isinstance(normalized.get("content"), str):
            normalized["content"] = restore_literal_newlines(normalized["content"])
        answers = normalized.get("answers")
        if isinstance(answers, list):
            for answer in answers:
                if isinstance(answer, dict) and isinstance(answer.get("content"), str):
                    answer["content"] = restore_literal_newlines(answer["content"])
        normalized["language"] = normalize_question_language(item.get("language"))
        normalized["difficulty"] = normalize_question_difficulty(item.get("difficulty"))
        out.append(normalized)
    return out


def _picture_note(figure_count: int, carried_count: int) -> str:
    figure_count = max(int(figure_count or 0), 0)
    carried_count = max(int(carried_count or 0), 0)
    if figure_count <= 0 and carried_count <= 0:
        return (
            'This page has no extracted pictures. '
            'Set "figures" and "carried_figures" to [] on every problem.'
        )
    parts: list[str] = []
    if figure_count:
        last = figure_count - 1
        parts.append(
            f"This page has {figure_count} extracted pictures, numbered 0 to {last} from top to bottom. "
            'Put the ids that belong to a problem in that problem\'s "figures" list.'
        )
    else:
        parts.append('This page has no new pictures. Set "figures" to [].')
    if carried_count:
        last = carried_count - 1
        parts.append(
            f"The previous page left {carried_count} picture(s) with the unfinished fragment, "
            f"numbered 0 to {last}. If the completed question uses them, list those ids in "
            '"carried_figures". Otherwise [].'
        )
    else:
        parts.append('No pictures were carried from the previous page. Set "carried_figures" to [].')
    parts.append(
        'If a question is cut off and its picture is on this page, start "unextracted" with [[FIGURE_n]] for each id.'
    )
    return " ".join(parts)


def parse_questions_page(
    text: str,
    carryover: str = "",
    *,
    figure_count: int = 0,
    carried_count: int = 0,
) -> str:
    client = _client()
    user = _picture_note(figure_count, carried_count) + "\n\n" + _prepend_carryover(str(text), carryover)
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_TEXT},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content.strip()


def parse_questions_page_image(
    png_bytes: bytes,
    page_number: int,
    supplementary_text: str = "",
    carryover: str = "",
    *,
    figure_count: int = 0,
    carried_count: int = 0,
) -> str:
    """Parse one PDF page from its rendered image (vision)."""
    client = _client()
    b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    user_parts: list[dict] = [
        {
            "type": "text",
            "text": (
                f"This is page {page_number} of the document. "
                "Extract every complete multiple-choice question. "
                "If a question is cut off at the bottom, put that fragment in \"unextracted\" "
                "instead of dropping it. "
                "Wrap ALL math in $...$ in both the question stem and every answer option. "
                + _picture_note(figure_count, carried_count)
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        },
    ]
    fragment = (carryover or "").strip()
    if fragment:
        user_parts.insert(
            0,
            {
                "type": "text",
                "text": (
                    "UNEXTRACTED FROM THE PREVIOUS PAGE. Prepend this to the current page: "
                    "it is the start of a question that was cut off and continues at the top "
                    "of this page image. Merge them into one question when the continuation "
                    "completes it. Do not drop this fragment.\n"
                    + fragment[:8000]
                ),
            },
        )
    if supplementary_text.strip():
        insert_at = 2 if fragment else 1
        user_parts.insert(
            insert_at,
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
    carryover: str = "",
    *,
    figure_count: int = 0,
    carried_count: int = 0,
) -> tuple[list[dict], str]:
    raw = parse_questions_page_image(
        png_bytes,
        page_number,
        supplementary_text,
        carryover,
        figure_count=figure_count,
        carried_count=carried_count,
    )
    return _split_page_payload(raw)


def parse_questions_page_to_list(
    text: str,
    carryover: str = "",
    *,
    figure_count: int = 0,
    carried_count: int = 0,
) -> tuple[list[dict], str]:
    raw = parse_questions_page(
        text,
        carryover,
        figure_count=figure_count,
        carried_count=carried_count,
    )
    return _split_page_payload(raw)
