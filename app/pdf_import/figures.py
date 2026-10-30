"""Find booklet figures on a rasterized page and pin them into the OCR text."""

from __future__ import annotations

import base64
import io
import re
from typing import Iterable

from PIL import Image

CELL = 12
INK_RATIO = 0.08
MIN_CELLS = 18
MIN_PX = 70
HEADER_WIDTH = 0.92
HEADER_HEIGHT = 0.12
TEXT_PAD_PX = 18
CROP_PAD_PX = 2
MAX_SIDE = 900
INK_THRESHOLD = 235
GAP_INK_RATIO = 0.02
MIN_GAP_PX = 8
MAX_CAPTION_PX = 150
MAX_CAPTION_FRAC = 0.30
HEADER_BAND = 0.11
FOOTER_BAND = 0.88
# Isolated digits/letters belong to diagrams; punching them fragments figures.
_DIAGRAM_LABEL = re.compile(r"^(\(?[0-9]{1,4}\)?|\?)$")


def vision_box_to_px(box: Iterable[float], size: tuple[int, int]) -> tuple[float, float, float, float]:
    """Apple Vision box (origin bottom-left, normalized) → pixel left, top, width, height."""
    width, height = size
    x, y, bw, bh = list(box)[:4]
    return x * width, (1.0 - y - bh) * height, bw * width, bh * height


def vision_top(box: Iterable[float]) -> float:
    x, y, bw, bh = list(box)[:4]
    return 1.0 - y - bh


def crop_to_data_url(image: Image.Image, box: tuple[int, int, int, int]) -> str:
    crop = image.crop(box).convert("RGB")
    crop.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=78, optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def tight_ink_box(
    gray: Image.Image, box: tuple[int, int, int, int], threshold: int = INK_THRESHOLD
) -> tuple[int, int, int, int]:
    px0, py0, px1, py1 = box
    region = gray.crop((px0, py0, px1, py1))
    mask = region.point(lambda pixel: 255 if pixel < threshold else 0)
    ink = mask.getbbox()
    if not ink:
        return box
    left, top, right, bottom = ink
    width, height = gray.size
    return (
        max(px0 + left - CROP_PAD_PX, 0),
        max(py0 + top - CROP_PAD_PX, 0),
        min(px0 + right + CROP_PAD_PX, width),
        min(py0 + bottom + CROP_PAD_PX, height),
    )


def _row_ink_ratios(
    gray: Image.Image, box: tuple[int, int, int, int]
) -> list[float]:
    px0, py0, px1, py1 = box
    region = gray.crop((px0, py0, px1, py1))
    pixels = region.load()
    width = max(px1 - px0, 1)
    height = max(py1 - py0, 0)
    step = 2
    totals = max((width + step - 1) // step, 1)
    ratios: list[float] = []
    for y in range(height):
        dark = 0
        for x in range(0, width, step):
            if pixels[x, y] < INK_THRESHOLD:
                dark += 1
        ratios.append(dark / totals)
    return ratios


def peel_trailing_text(
    gray: Image.Image, box: tuple[int, int, int, int]
) -> tuple[tuple[int, int, int, int], list[tuple[int, int, int, int]]]:
    """Split answer/caption lines that sit under a figure across a white gap."""
    px0, py0, px1, py1 = box
    ratios = _row_ink_ratios(gray, box)
    height = len(ratios)
    if height < MIN_PX + MIN_GAP_PX + 12:
        return box, []

    gaps: list[tuple[int, int]] = []
    index = 0
    while index < height:
        if ratios[index] <= GAP_INK_RATIO:
            end = index + 1
            while end < height and ratios[end] <= GAP_INK_RATIO:
                end += 1
            if end - index >= MIN_GAP_PX:
                gaps.append((index, end))
            index = end
        else:
            index += 1

    leftovers: list[tuple[int, int, int, int]] = []
    fig_bottom = height
    for gap_top, gap_bottom in reversed(gaps):
        caption_h = fig_bottom - gap_bottom
        body_h = gap_top
        if caption_h <= 0:
            fig_bottom = gap_top
            continue
        too_tall = caption_h > MAX_CAPTION_PX and caption_h > MAX_CAPTION_FRAC * height
        if too_tall or body_h < MIN_PX:
            break
        leftover = tight_ink_box(
            gray, (px0, py0 + gap_bottom, px1, py0 + fig_bottom)
        )
        if leftover[3] - leftover[1] >= 8:
            leftovers.append(leftover)
        fig_bottom = gap_top

    if not leftovers:
        return box, []

    peeled = tight_ink_box(gray, (px0, py0, px1, py0 + fig_bottom))
    if peeled[2] - peeled[0] < MIN_PX or peeled[3] - peeled[1] < MIN_PX:
        return box, []
    leftovers.reverse()
    return peeled, leftovers


def detect_figures(
    image: Image.Image, ocr_lines
) -> tuple[list[dict], list[tuple[int, int, int, int]]]:
    width, height = image.size
    gray = image.convert("L")
    cols = max(width // CELL, 1)
    rows = max(height // CELL, 1)
    ink = [[False] * cols for _ in range(rows)]
    pixels = gray.load()

    for row in range(rows):
        for col in range(cols):
            x0, y0 = col * CELL, row * CELL
            x1, y1 = min(x0 + CELL, width), min(y0 + CELL, height)
            dark = 0
            total = 0
            for y in range(y0, y1, 2):
                for x in range(x0, x1, 2):
                    total += 1
                    if pixels[x, y] < 235:
                        dark += 1
            ink[row][col] = bool(total) and (dark / total) > INK_RATIO

    for text, _conf, box in ocr_lines:
        if _is_diagram_label(text):
            continue
        left, top, box_w, box_h = vision_box_to_px(box, (width, height))
        c0 = max(int((left - TEXT_PAD_PX) / CELL), 0)
        r0 = max(int((top - TEXT_PAD_PX) / CELL), 0)
        c1 = min(int((left + box_w + TEXT_PAD_PX) / CELL), cols - 1)
        r1 = min(int((top + box_h + TEXT_PAD_PX) / CELL), rows - 1)
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                ink[row][col] = False

    seen = [[False] * cols for _ in range(rows)]
    components: list[list[tuple[int, int]]] = []
    neighbors = (
        (1, 0),
        (-1, 0),
        (0, 1),
        (0, -1),
        (1, 1),
        (1, -1),
        (-1, 1),
        (-1, -1),
    )
    for row in range(rows):
        for col in range(cols):
            if not ink[row][col] or seen[row][col]:
                continue
            stack = [(row, col)]
            seen[row][col] = True
            cells: list[tuple[int, int]] = []
            while stack:
                cr, cc = stack.pop()
                cells.append((cr, cc))
                for dr, dc in neighbors:
                    nr, nc = cr + dr, cc + dc
                    if 0 <= nr < rows and 0 <= nc < cols and ink[nr][nc] and not seen[nr][nc]:
                        seen[nr][nc] = True
                        stack.append((nr, nc))
            components.append(cells)

    figures: list[dict] = []
    leftover_boxes: list[tuple[int, int, int, int]] = []
    for cells in components:
        if len(cells) < MIN_CELLS:
            continue
        rows_i = [cell[0] for cell in cells]
        cols_i = [cell[1] for cell in cells]
        r0, r1 = min(rows_i), max(rows_i)
        c0, c1 = min(cols_i), max(cols_i)
        px0, py0 = c0 * CELL, r0 * CELL
        px1, py1 = min((c1 + 1) * CELL, width), min((r1 + 1) * CELL, height)
        fig_w, fig_h = px1 - px0, py1 - py0
        if fig_w < MIN_PX or fig_h < MIN_PX:
            continue
        if fig_w > HEADER_WIDTH * width and fig_h < HEADER_HEIGHT * height:
            continue
        if fig_h > HEADER_WIDTH * height and fig_w < HEADER_HEIGHT * width:
            continue
        box_cells = (r1 - r0 + 1) * (c1 - c0 + 1)
        if box_cells and len(cells) / box_cells < 0.15:
            continue
        crop = tight_ink_box(gray, (px0, py0, px1, py1))
        crop, captions = peel_trailing_text(gray, crop)
        leftover_boxes.extend(captions)
        left, top, right, bottom = crop
        if _is_chrome_box(left, top, right, bottom, width, height):
            continue
        figures.append(
            {
                "y": top / height,
                "box": (left / width, top / height, right / width, bottom / height),
                "data_url": crop_to_data_url(image, crop),
            }
        )

    figures.sort(key=lambda item: item["y"])
    for index, figure in enumerate(figures):
        figure["id"] = index
    leftover_boxes.sort(key=lambda item: item[1])
    figures = refine_figures(image, figures, ocr_lines)
    return figures, leftover_boxes


def refine_figures(image: Image.Image, figures: list[dict], ocr_lines) -> list[dict]:
    """Grow crops around nearby diagram labels and merge neighbouring figures."""
    if not figures:
        return figures
    width, height = image.size
    refined: list[dict] = []
    for figure in figures:
        box = _absorb_labels(list(figure["box"]), ocr_lines)
        refined.append({**figure, "box": tuple(box), "y": box[1]})
    refined = _merge_nearby_figures(refined)
    for index, figure in enumerate(refined):
        x0, y0, x1, y1 = figure["box"]
        crop = (
            max(int(x0 * width), 0),
            max(int(y0 * height), 0),
            min(int(x1 * width) + 1, width),
            min(int(y1 * height) + 1, height),
        )
        if crop[2] - crop[0] < MIN_PX or crop[3] - crop[1] < MIN_PX:
            continue
        figure["data_url"] = crop_to_data_url(image, crop)
        figure["y"] = crop[1] / height
        figure["box"] = (
            crop[0] / width,
            crop[1] / height,
            crop[2] / width,
            crop[3] / height,
        )
        figure["id"] = index
    return [figure for figure in refined if figure.get("data_url")]


def _absorb_labels(box: list[float], ocr_lines, pad: float = 0.03) -> list[float]:
    x0, y0, x1, y1 = box
    ox0, oy0, ox1, oy1 = box
    for text, _conf, word_box in ocr_lines:
        token = (text or "").strip()
        if not _is_diagram_label(token):
            continue
        left, _y, width, height = list(word_box)[:4]
        if width > 0.08 or height > 0.045:
            continue
        top = vision_top(word_box)
        right, bottom = left + width, top + height
        cx, cy = left + width / 2, top + height / 2
        if not (ox0 - pad <= cx <= ox1 + pad and oy0 - pad <= cy <= oy1 + pad):
            continue
        x0, y0 = min(x0, left), min(y0, top)
        x1, y1 = max(x1, right), max(y1, bottom)
    return [x0, y0, x1, y1]


def _merge_nearby_figures(figures: list[dict], gap: float = 0.012) -> list[dict]:
    if len(figures) < 2:
        return figures
    boxes = [list(item["box"]) for item in figures]
    used = [False] * len(boxes)
    merged: list[dict] = []
    for i, box in enumerate(boxes):
        if used[i]:
            continue
        used[i] = True
        x0, y0, x1, y1 = box
        grew = True
        while grew:
            grew = False
            for j, other in enumerate(boxes):
                if used[j]:
                    continue
                if _boxes_near((x0, y0, x1, y1), other, gap):
                    used[j] = True
                    x0 = min(x0, other[0])
                    y0 = min(y0, other[1])
                    x1 = max(x1, other[2])
                    y1 = max(y1, other[3])
                    grew = True
        merged.append({"box": [x0, y0, x1, y1], "y": y0, "data_url": ""})
    merged.sort(key=lambda item: item["y"])
    return merged


def _boxes_near(a: list[float], b: list[float], gap: float) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (
        ax1 + gap < bx0 or bx1 + gap < ax0 or ay1 + gap < by0 or by1 + gap < ay0
    )


def _is_diagram_label(text: str | None) -> bool:
    return bool(_DIAGRAM_LABEL.fullmatch((text or "").strip()))


def _is_chrome_box(
    left: int, top: int, right: int, bottom: int, width: int, height: int
) -> bool:
    """Drop header logos and footer bars so they are not treated as problem art."""
    if height <= 0 or width <= 0:
        return False
    top_n, bottom_n = top / height, bottom / height
    fig_w, fig_h = right - left, bottom - top
    if bottom_n <= HEADER_BAND:
        return True
    if top_n >= FOOTER_BAND:
        return True
    if top_n < HEADER_BAND and fig_h < 0.10 * height:
        return True
    if fig_w > HEADER_WIDTH * width and fig_h < HEADER_HEIGHT * height:
        return True
    return False


def page_text_with_figures(ocr_lines, figures: list[dict]) -> str:
    items: list[tuple[float, float, str, object]] = []
    for text, _conf, box in ocr_lines:
        stripped = (text or "").strip()
        if not stripped:
            continue
        items.append((vision_top(box), float(box[0]), "text", stripped))
    for figure in figures:
        items.append((float(figure["y"]), 0.0, "figure", int(figure["id"])))
    items.sort(key=lambda item: (round(item[0] * 80), item[1]))

    lines: list[str] = []
    current: list[str] = []
    current_band: int | None = None
    for top, _x, kind, value in items:
        band = round(top * 80)
        if kind == "figure":
            if current:
                lines.append(" ".join(current))
                current = []
            lines.append(f"[[FIGURE_{value}]]")
            current_band = None
            continue
        if current_band is None or abs(band - current_band) <= 1:
            current.append(str(value))
            if current_band is None:
                current_band = band
        else:
            lines.append(" ".join(current))
            current = [str(value)]
            current_band = band
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)
