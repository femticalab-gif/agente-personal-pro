"""Extracción de texto de documentos (PDF, DOCX, XLSX, texto)."""

from __future__ import annotations

import csv
import io
import logging

from marc_agent.integrations import IntegrationError

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def extract_text(data: bytes, mime: str, max_chars: int = 50_000) -> str:
    if len(data) > MAX_BYTES:
        raise IntegrationError("Documento demasiado grande (>20 MB)")
    try:
        if mime == PDF or data[:5] == b"%PDF-":
            text = _pdf(data)
        elif mime == DOCX:
            text = _docx(data)
        elif mime == XLSX:
            text = _xlsx(data)
        elif mime.startswith("text/") or mime in {"application/json", "application/csv"}:
            text = data.decode("utf-8", errors="replace")
        else:
            raise IntegrationError(f"Tipo de documento no soportado: {mime}")
    except IntegrationError:
        raise
    except Exception as exc:
        log.warning("docparse_failed", extra={"event": "docparse_failed", "mime": mime})
        raise IntegrationError(f"No se pudo leer el documento ({type(exc).__name__})") from exc
    return text[:max_chars]


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages[:200]]
    return "\n\n".join(pages).strip()


def _docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts).strip()


def _xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = io.StringIO()
    writer = csv.writer(out)
    for sheet in workbook.worksheets[:10]:
        out.write(f"# Hoja: {sheet.title}\n")
        for i, row in enumerate(sheet.iter_rows(values_only=True)):
            if i >= 2000:
                break
            writer.writerow(["" if v is None else v for v in row])
    workbook.close()
    return out.getvalue().strip()
