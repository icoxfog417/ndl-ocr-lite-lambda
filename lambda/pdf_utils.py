"""PDF-to-image rendering via pypdfium2."""

from __future__ import annotations

import os

import pypdfium2 as pdfium

from input_parser import parse_pages


def render_pdf_pages(
    pdf_data: bytes, work_dir: str, pages_param: str | None = None
) -> list[str]:
    """Render selected PDF pages to JPEG images.

    Args:
        pdf_data: Raw PDF file bytes.
        work_dir: Directory to write page images into.
        pages_param: Page selection string (e.g. '1-3', '1,3,5'). None = all pages.

    Returns:
        List of paths to rendered JPEG images, in page order.
    """
    pdf = pdfium.PdfDocument(pdf_data)
    total_pages = len(pdf)
    page_indices = parse_pages(pages_param, total_pages)

    image_paths: list[str] = []
    for idx in page_indices:
        page = pdf[idx]
        bitmap = page.render(scale=300 / 72)  # 300 DPI
        pil_img = bitmap.to_pil().convert("RGB")
        img_path = os.path.join(work_dir, f"page_{idx + 1:03d}.jpg")
        pil_img.save(img_path, "JPEG")
        image_paths.append(img_path)

    pdf.close()
    return image_paths
