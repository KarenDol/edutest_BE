"""Crop figures off a PDF page and store them under /uploads/."""

from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path

import fitz
from PIL import Image

from app.pdf_import.figures import crop_to_data_url, detect_figures

# Same directory StaticFiles serves in app.main. Kept here to avoid importing the app.
_UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "data" / "uploads"
_MIN_WORDS_FOR_INK = 12
_MAX_FIGURES = 8
_MAX_AREA = 0.50
_EMBEDDED_MAX_AREA = 0.72
_MIN_CROP_PX = 70


def prepare_pdf_pages(filepath: str, *, dpi: int | None = None) -> tuple[list[bytes], list[list[dict]]]:
    """Render each page to PNG and return figure records ({url, y}) beside it."""
    from app.config import settings

    if dpi is None:
        dpi = settings.pdf_import_page_dpi
    doc = fitz.open(filepath)
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    pngs: list[bytes] = []
    figures: list[list[dict]] = []
    try:
        _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            png = pix.tobytes("png")
            pngs.append(png)
            try:
                figures.append(_figures_for_page(page, png))
            except Exception:
                figures.append([])
    finally:
        doc.close()
    return pngs, figures


def _figures_for_page(page: fitz.Page, png: bytes) -> list[dict]:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    words = _pdf_words_as_vision(page)
    found: list[dict] = []
    # Without word boxes, body text is one ink blob. Only punch text out when the page has a text layer.
    if len(words) >= _MIN_WORDS_FOR_INK:
        detected, _leftovers = detect_figures(image, words)
        for figure in detected:
            box = figure.get("box")
            if not box or _area(box) > _MAX_AREA:
                continue
            raw = _data_url_bytes(figure.get("data_url") or "")
            if not raw:
                continue
            found.append({"y": float(figure.get("y") or box[1]), "box": tuple(box), "jpeg": raw})

    for box in _embedded_boxes(page):
        if _area(box) > _EMBEDDED_MAX_AREA:
            continue
        if any(_iou(box, item["box"]) > 0.45 for item in found):
            continue
        crop = _crop_box(image, box)
        if crop is None:
            continue
        raw = _data_url_bytes(crop_to_data_url(image, crop))
        if not raw:
            continue
        found.append({"y": box[1], "box": box, "jpeg": raw})

    found.sort(key=lambda item: item["y"])
    saved: list[dict] = []
    for item in found[:_MAX_FIGURES]:
        saved.append({"url": _save_jpeg(item["jpeg"]), "y": item["y"]})
    return saved


def _pdf_words_as_vision(page: fitz.Page) -> list[tuple[str, float, list[float]]]:
    """PDF word boxes as Apple-Vision boxes: origin bottom-left, normalized."""
    rect = page.rect
    width, height = float(rect.width), float(rect.height)
    if width <= 0 or height <= 0:
        return []
    words: list[tuple[str, float, list[float]]] = []
    for x0, y0, x1, y1, word, *_rest in page.get_text("words"):
        text = (word or "").strip()
        if not text:
            continue
        words.append(
            (
                text,
                1.0,
                [
                    x0 / width,
                    1.0 - (y1 / height),
                    (x1 - x0) / width,
                    (y1 - y0) / height,
                ],
            )
        )
    return words


def _embedded_boxes(page: fitz.Page) -> list[tuple[float, float, float, float]]:
    rect = page.rect
    width, height = float(rect.width), float(rect.height)
    if width <= 0 or height <= 0:
        return []
    try:
        infos = page.get_image_info()
    except Exception:
        return []
    boxes: list[tuple[float, float, float, float]] = []
    for info in infos or []:
        bbox = info.get("bbox") if isinstance(info, dict) else None
        if not bbox or len(bbox) < 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        if x1 - x0 < 36 or y1 - y0 < 36:
            continue
        boxes.append((x0 / width, y0 / height, x1 / width, y1 / height))
    return boxes


def _crop_box(image: Image.Image, box: tuple[float, float, float, float]) -> tuple[int, int, int, int] | None:
    width, height = image.size
    x0, y0, x1, y1 = box
    px0 = max(int(x0 * width), 0)
    py0 = max(int(y0 * height), 0)
    px1 = min(int(x1 * width) + 1, width)
    py1 = min(int(y1 * height) + 1, height)
    if px1 - px0 < _MIN_CROP_PX or py1 - py0 < _MIN_CROP_PX:
        return None
    return (px0, py0, px1, py1)


def _area(box: tuple[float, ...]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    union = _area(a) + _area(b) - inter
    return inter / union if union else 0.0


def _data_url_bytes(url: str) -> bytes:
    if "," not in url:
        return b""
    return base64.b64decode(url.split(",", 1)[1])


def _save_jpeg(raw: bytes) -> str:
    name = f"{uuid.uuid4().hex}.jpg"
    (_UPLOAD_ROOT / name).write_bytes(raw)
    return f"/uploads/{name}"
