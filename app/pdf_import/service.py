import asyncio
import json
import os
from collections.abc import AsyncIterator

from app.pdf_import.attach_figures import attach_question_figures
from app.pdf_import.extract_text import extract_text
from app.pdf_import.parse_questions import (
    parse_questions_page_image_to_list,
    parse_questions_page_to_list,
)
from app.pdf_import.page_figures import prepare_pdf_pages


async def stream_parsed_questions(
    tmp_path: str,
    *,
    disconnected: asyncio.Event | None = None,
) -> AsyncIterator[bytes]:
    ext = os.path.splitext(tmp_path)[1].lower()
    try:
        if ext == ".pdf":
            async for chunk in _stream_pdf_pages(tmp_path, disconnected=disconnected):
                yield chunk
        else:
            async for chunk in _stream_text_pages(tmp_path, disconnected=disconnected):
                yield chunk
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


async def _stream_pdf_pages(
    tmp_path: str,
    *,
    disconnected: asyncio.Event | None,
) -> AsyncIterator[bytes]:
    prepared, page_texts = await asyncio.gather(
        asyncio.to_thread(prepare_pdf_pages, tmp_path),
        asyncio.to_thread(extract_text, tmp_path),
    )
    page_images, page_figure_sets = prepared
    total = len(page_images)

    yield _line({"type": "progress", "current": 0, "total": total, "percent": 0})

    # Incomplete question cut off at the bottom of a page, prepended to the next page.
    carryover = ""
    carry_figures: list[dict] = []
    for idx, png in enumerate(page_images, start=1):
        if disconnected and disconnected.is_set():
            break

        hint = page_texts[idx - 1] if idx - 1 < len(page_texts) else ""
        page_figures = page_figure_sets[idx - 1] if idx - 1 < len(page_figure_sets) else []
        try:
            questions, carryover = await asyncio.to_thread(
                parse_questions_page_image_to_list,
                png,
                idx,
                hint,
                carryover,
                figure_count=len(page_figures),
                carried_count=len(carry_figures),
            )
            questions, carryover, carry_figures = attach_question_figures(
                questions, page_figures, carry_figures, carryover
            )
        except json.JSONDecodeError as e:
            yield _line({"type": "error", "message": f"Page {idx}: invalid AI response ({e})"})
            continue
        except Exception as e:
            yield _line({"type": "error", "message": f"Page {idx}: {e}"})
            continue

        for q in questions:
            if disconnected and disconnected.is_set():
                break
            yield _line(q)

        percent = int(round((idx / total) * 100)) if total else 100
        yield _line(
            {"type": "progress", "current": idx, "total": total, "percent": percent}
        )
        await asyncio.sleep(0)

    if carryover.strip() and not (disconnected and disconnected.is_set()):
        async for chunk in _emit_carryover(
            carryover, carry_figures, disconnected=disconnected
        ):
            yield chunk


async def _stream_text_pages(
    tmp_path: str,
    *,
    disconnected: asyncio.Event | None,
) -> AsyncIterator[bytes]:
    pages = await asyncio.to_thread(extract_text, tmp_path)
    total = len(pages)

    yield _line({"type": "progress", "current": 0, "total": total, "percent": 0})

    carryover = ""
    for idx, page in enumerate(pages, start=1):
        if disconnected and disconnected.is_set():
            break

        try:
            questions, carryover = await asyncio.to_thread(
                parse_questions_page_to_list, page, carryover
            )
            questions, carryover, _unused = attach_question_figures(
                questions, [], [], carryover
            )
        except json.JSONDecodeError as e:
            yield _line({"type": "error", "message": f"Chunk {idx}: invalid AI response ({e})"})
            continue
        except Exception as e:
            yield _line({"type": "error", "message": f"Chunk {idx}: {e}"})
            continue

        for q in questions:
            if disconnected and disconnected.is_set():
                break
            yield _line(q)

        percent = int(round((idx / total) * 100)) if total else 100
        yield _line(
            {"type": "progress", "current": idx, "total": total, "percent": percent}
        )
        await asyncio.sleep(0)

    if carryover.strip() and not (disconnected and disconnected.is_set()):
        async for chunk in _emit_carryover(carryover, [], disconnected=disconnected):
            yield chunk


async def _emit_carryover(
    carryover: str,
    carry_figures: list[dict],
    *,
    disconnected: asyncio.Event | None,
) -> AsyncIterator[bytes]:
    """Last page left an unfinished fragment; try to parse it on its own."""
    try:
        questions, leftover = await asyncio.to_thread(
            parse_questions_page_to_list,
            carryover,
            carried_count=len(carry_figures),
        )
        questions, _leftover, _unused = attach_question_figures(
            questions, [], carry_figures, leftover
        )
    except json.JSONDecodeError as e:
        yield _line({"type": "error", "message": f"Page break remainder: invalid AI response ({e})"})
        return
    except Exception as e:
        yield _line({"type": "error", "message": f"Page break remainder: {e}"})
        return

    for q in questions:
        if disconnected and disconnected.is_set():
            break
        yield _line(q)


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
