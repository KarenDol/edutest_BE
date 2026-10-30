"""Question difficulty levels A, B, and C (stored on questions and returned by PDF import)."""

import re

QUESTION_DIFFICULTIES = frozenset({"A", "B", "C"})
QUESTION_DIFFICULTY_DEFAULT = "B"

# Cyrillic letters that look like the Latin level marks used in Kazakh/Russian tests.
_CYRILLIC_LOOKALIKES = str.maketrans(
    {
        "А": "A",
        "а": "A",
        "В": "B",
        "в": "B",
        "С": "C",
        "с": "C",
    }
)

# A level mark is a standalone A/B/C, not a letter buried in a word ("уровень" contains в).
_STANDALONE_MARK = re.compile(r"(?<![\w])([ABCabc])(?![\w])", re.UNICODE)


def normalize_question_difficulty(raw: str | None) -> str:
    if raw is None:
        return QUESTION_DIFFICULTY_DEFAULT
    text = str(raw).strip().translate(_CYRILLIC_LOOKALIKES)
    if not text:
        return QUESTION_DIFFICULTY_DEFAULT
    marks = _STANDALONE_MARK.findall(text)
    if marks:
        return marks[-1].upper()
    return QUESTION_DIFFICULTY_DEFAULT
