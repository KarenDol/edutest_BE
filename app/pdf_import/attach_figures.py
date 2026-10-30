"""Pin cropped figure files onto the questions the model assigned them to."""

from __future__ import annotations

import re

_FIGURE_TOKEN = re.compile(r"\[\[FIGURE_(\d+)\]\]", re.IGNORECASE)
_PICTURE_HINT = re.compile(
    r"сурет|сызба|диаграмм|схема|график|кесте|(?<!\w)карта(?!\w)|figure|diagram|graph|picture",
    re.IGNORECASE,
)


def attach_question_figures(
    questions: list,
    page_figures: list[dict],
    carry_figures: list[dict],
    unextracted: str,
) -> tuple[list, str, list[dict]]:
    """Set image_urls on each question. Return the fragment and pictures to carry forward."""
    page_figures = list(page_figures or [])
    carry_figures = list(carry_figures or [])
    used_page: set[int] = set()
    used_carry: set[int] = set()

    for question in questions:
        if not isinstance(question, dict):
            continue
        content, content_ids = _strip_tokens(str(question.get("content") or ""))
        question["content"] = content
        answers = question.get("answers")
        if isinstance(answers, list):
            for answer in answers:
                if isinstance(answer, dict) and isinstance(answer.get("content"), str):
                    cleaned, _ids = _strip_tokens(answer["content"])
                    answer["content"] = cleaned
        page_ids = _id_list(question.pop("figures", [])) + content_ids
        carry_ids = _id_list(question.pop("carried_figures", []))
        urls: list[str] = []
        for index in carry_ids:
            _take(carry_figures, index, used_carry, urls)
        for index in page_ids:
            _take(page_figures, index, used_page, urls)
        question["image_urls"] = urls

    unused_carry = [i for i in range(len(carry_figures)) if i not in used_carry]
    unused_page = [i for i in range(len(page_figures)) if i not in used_page]
    needy = [
        question
        for question in questions
        if isinstance(question, dict)
        and not question.get("image_urls")
        and _PICTURE_HINT.search(str(question.get("content") or ""))
    ]
    pool = [("carry", i) for i in unused_carry] + [("page", i) for i in unused_page]
    for question, (kind, index) in zip(needy, pool):
        source = carry_figures if kind == "carry" else page_figures
        question["image_urls"] = [source[index]["url"]]
        (used_carry if kind == "carry" else used_page).add(index)

    cleaned, mentioned = _strip_tokens(unextracted or "")
    next_carry: list[dict] = []
    if cleaned:
        if mentioned:
            next_carry = [
                page_figures[i]
                for i in mentioned
                if 0 <= i < len(page_figures) and i not in used_page
            ]
        else:
            next_carry = [
                page_figures[i]
                for i in unused_page
                if i not in used_page and float(page_figures[i].get("y") or 0) >= 0.40
            ][:3]
        for index, figure in enumerate(carry_figures):
            if index not in used_carry:
                next_carry.append(figure)

    seen: set[str] = set()
    unique: list[dict] = []
    for figure in next_carry:
        url = figure.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(figure)
    return questions, cleaned, unique


def _take(figures: list[dict], index: int, used: set[int], urls: list[str]) -> None:
    if index in used or index < 0 or index >= len(figures):
        return
    url = figures[index].get("url") or ""
    if not url or url in urls:
        return
    used.add(index)
    urls.append(url)


def _id_list(value) -> list[int]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, str):
        return [int(part) for part in re.findall(r"\d+", value)]
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            out.append(int(item))
        elif isinstance(item, str) and item.strip().isdigit():
            out.append(int(item.strip()))
    return out


def _strip_tokens(text: str) -> tuple[str, list[int]]:
    ids = [int(number) for number in _FIGURE_TOKEN.findall(text or "")]
    cleaned = _FIGURE_TOKEN.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, ids
