import os

from openpyxl import load_workbook
from pypdf import PdfReader


def extract_text(filepath: str) -> list[str]:
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".pdf":
        return extract_pdf(filepath)
    if ext == ".xlsx":
        return extract_xlsx(filepath)
    raise ValueError(f"Unsupported file type: {ext}")


def extract_xlsx(filepath: str) -> list[str]:
    wb = load_workbook(filepath)
    sheet = wb.active
    text = ""
    for row in sheet.iter_rows(values_only=True):
        if all(v is None for v in row):
            continue
        row = list(row)
        while row and row[-1] is None:
            row.pop()
        row_text = " | ".join(str(v) for v in row if v is not None)
        text += row_text + "\n"
    return [text]


def extract_pdf(filepath: str) -> list[str]:
    reader = PdfReader(filepath)
    text_chunks: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        text_chunks.append(page_text)
    return text_chunks
