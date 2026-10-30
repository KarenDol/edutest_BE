"""Allowed question languages (stored on questions and returned by PDF import)."""

QUESTION_LANGUAGES = frozenset({"kazakh", "russian", "english"})
QUESTION_LANGUAGE_DEFAULT = "russian"

_LANGUAGE_ALIASES: dict[str, str] = {
    "kazakh": "kazakh",
    "kk": "kazakh",
    "kaz": "kazakh",
    "қазақ": "kazakh",
    "казахский": "kazakh",
    "russian": "russian",
    "ru": "russian",
    "rus": "russian",
    "русский": "russian",
    "рус": "russian",
    "english": "english",
    "en": "english",
    "eng": "english",
    "английский": "english",
}


def normalize_question_language(raw: str | None) -> str:
    if raw is None:
        return QUESTION_LANGUAGE_DEFAULT
    key = str(raw).strip().lower().replace("ё", "е")
    if not key:
        return QUESTION_LANGUAGE_DEFAULT
    if key in QUESTION_LANGUAGES:
        return key
    return _LANGUAGE_ALIASES.get(key, QUESTION_LANGUAGE_DEFAULT)
