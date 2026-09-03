"""Turn drawing PDFs into images for the vision call (spec stage 2).

150-200 DPI is the band the spec asks for: enough to read a title block and a
tolerance callout, not so much that a multi-page drawing blows the request
size. Rendering is done with PyMuPDF; `pdftoppm` is the documented alternative
and would drop straight in behind the same function.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from app.config import get_settings
from app.services.ai import ImageBlock

logger = logging.getLogger(__name__)

#: Anthropic's per-request image budget. A drawing set longer than this is
#: split rather than silently truncated.
MAX_PAGES_PER_CALL = 20


class RasteriseError(Exception):
    pass


@dataclass
class RasterisedPage:
    page_number: int
    png_bytes: bytes

    def to_image_block(self, total_pages: int) -> ImageBlock:
        return ImageBlock(
            base64_data=base64.standard_b64encode(self.png_bytes).decode("ascii"),
            media_type="image/png",
            label=f"Page {self.page_number} of {total_pages}",
        )


def rasterise_pdf(pdf_bytes: bytes, dpi: int | None = None) -> list[RasterisedPage]:
    """Render every page of a PDF to PNG at the configured DPI."""
    dpi = dpi or get_settings().drawing_dpi
    if not 72 <= dpi <= 400:
        raise RasteriseError(f"DPI {dpi} is outside the sensible 72-400 range")
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RasteriseError("PyMuPDF is not installed") from exc

    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise RasteriseError(f"Could not open PDF: {exc}") from exc

    pages: list[RasterisedPage] = []
    with document:
        if document.page_count == 0:
            raise RasteriseError("PDF has no pages")
        for index, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(dpi=dpi)
            pages.append(RasterisedPage(page_number=index, png_bytes=pixmap.tobytes("png")))
    logger.info("Rasterised %d page(s) at %d DPI", len(pages), dpi)
    return pages


def rasterise_image(image_bytes: bytes, media_type: str) -> list[RasterisedPage]:
    """Pass a drawing that is already an image straight through."""
    if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        raise RasteriseError(f"Unsupported image type {media_type}")
    return [RasterisedPage(page_number=1, png_bytes=image_bytes)]


def to_image_blocks(pages: list[RasterisedPage]) -> list[ImageBlock]:
    """Build the vision content blocks, refusing to silently drop pages."""
    if len(pages) > MAX_PAGES_PER_CALL:
        raise RasteriseError(
            f"Drawing has {len(pages)} pages, above the {MAX_PAGES_PER_CALL}-page "
            "limit for a single call. Split it rather than truncating — a page "
            "dropped without anyone noticing is how a feature gets left out of "
            "a quote."
        )
    total = len(pages)
    return [page.to_image_block(total) for page in pages]
