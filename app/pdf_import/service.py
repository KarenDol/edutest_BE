import asyncio
import json
import os
from collections.abc import AsyncIterator

from app.pdf_import.extract_text import extract_text
from app.pdf_import.parse_questions import (
    parse_questions_page_image_to_list,
    parse_questions_page_to_list,
)
from app.pdf_import.render_pdf import render_pdf_pages


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
    page_images, page_texts = await asyncio.gather(
        asyncio.to_thread(render_pdf_pages, tmp_path),
        asyncio.to_thread(extract_text, tmp_path),
    )
    total = len(page_images)

    yield _line({"type": "progress", "current": 0, "total": total, "percent": 0})

    for idx, png in enumerate(page_images, start=1):
        if disconnected and disconnected.is_set():
            break

        hint = page_texts[idx - 1] if idx - 1 < len(page_texts) else ""
        try:
            questions = await asyncio.to_thread(
                parse_questions_page_image_to_list,
                png,
                idx,
                hint,
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


async def _stream_text_pages(
    tmp_path: str,
    *,
    disconnected: asyncio.Event | None,
) -> AsyncIterator[bytes]:
    pages = await asyncio.to_thread(extract_text, tmp_path)
    total = len(pages)

    yield _line({"type": "progress", "current": 0, "total": total, "percent": 0})

    for idx, page in enumerate(pages, start=1):
        if disconnected and disconnected.is_set():
            break

        try:
            questions = await asyncio.to_thread(parse_questions_page_to_list, page)
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


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
