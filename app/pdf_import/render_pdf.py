import fitz


def render_pdf_pages(filepath: str, *, dpi: int | None = None) -> list[bytes]:
    """Render each PDF page to PNG bytes for vision-based parsing."""
    from app.config import settings

    if dpi is None:
        dpi = settings.pdf_import_page_dpi
    doc = fitz.open(filepath)
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    pages: list[bytes] = []
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pages.append(pix.tobytes("png"))
    finally:
        doc.close()
    return pages
