"""Parse Lambda event: decode base64/S3 URI, detect PDF vs image, parse pages parameter."""

from __future__ import annotations

import base64
import io
import os
import re

import boto3
from PIL import Image

s3_client = boto3.client("s3")


def parse_pages(pages_str: str | None, total_pages: int) -> list[int]:
    """Parse a pages parameter string like '1-3', '1,3,5' into zero-based page indices.

    Returns sorted unique zero-based indices within [0, total_pages).
    """
    if not pages_str:
        return list(range(total_pages))

    indices: set[int] = set()
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str.strip())
            end = int(end_str.strip())
            for p in range(start, end + 1):
                if 1 <= p <= total_pages:
                    indices.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                indices.add(p - 1)

    return sorted(indices)


def _is_pdf(data: bytes) -> bool:
    """Check if data starts with PDF magic bytes."""
    return data[:5] == b"%PDF-"


def _is_s3_uri(image_str: str) -> bool:
    """Check if a string is an S3 URI."""
    return image_str.startswith("s3://")


def _download_s3(uri: str, dest_path: str) -> None:
    """Download an S3 object to a local file."""
    match = re.match(r"s3://([^/]+)/(.+)", uri)
    if not match:
        raise ValueError(f"Invalid S3 URI: {uri}")
    bucket, key = match.group(1), match.group(2)
    s3_client.download_file(bucket, key, dest_path)


def parse_input(event: dict, work_dir: str) -> tuple[list[str], bool]:
    """Parse the Lambda event and return (image_paths, is_pdf).

    For images: returns a single-element list with the image path.
    For PDFs: returns list of rendered page image paths.

    All files are written into work_dir.
    """
    image_str = event.get("image")
    if not image_str:
        raise ValueError("Missing required parameter: image")

    pages_param: str | None = event.get("pages")

    os.makedirs(work_dir, exist_ok=True)

    if _is_s3_uri(image_str):
        tmp_file = os.path.join(work_dir, "input_file")
        _download_s3(image_str, tmp_file)
        with open(tmp_file, "rb") as f:
            data = f.read()
    else:
        try:
            data = base64.b64decode(image_str)
        except Exception as e:
            raise ValueError(f"Failed to decode base64 image data: {e}")

    if _is_pdf(data):
        return _render_pdf(data, work_dir, pages_param), True
    else:
        img_path = os.path.join(work_dir, "page_001.jpg")
        _save_image(data, img_path)
        return [img_path], False


def _save_image(data: bytes, path: str) -> None:
    """Save raw image bytes as a JPEG file."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        raise ValueError(f"Cannot decode image data: {e}")
    img.save(path, "JPEG")


def _render_pdf(data: bytes, work_dir: str, pages_param: str | None) -> list[str]:
    """Render PDF pages to JPEG images using pypdfium2."""
    import pdf_utils

    return pdf_utils.render_pdf_pages(data, work_dir, pages_param)
