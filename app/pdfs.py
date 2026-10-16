import io

from loguru import logger

from .util import clean


def pdf_text(content: bytes, max_pages: int = 40) -> str:
    """Primary extractor: PyMuPDF."""
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        with fitz.open(stream=content, filetype="pdf") as doc:
            return "\n".join(p.get_text() for i, p in enumerate(doc) if i < max_pages)
    except Exception as e:
        logger.debug(f"pymupdf failed: {e}")
        return ""


def pdf_tables(content: bytes, max_pages: int = 30) -> list[list[list[str]]]:
    """pdfplumber: used when table structure is needed."""
    try:
        import pdfplumber
        out = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages):
                if i >= max_pages:
                    break
                for t in page.extract_tables():
                    out.append([[clean(c) for c in row] for row in t if row])
        return out
    except Exception as e:
        logger.debug(f"pdfplumber failed: {e}")
        return []