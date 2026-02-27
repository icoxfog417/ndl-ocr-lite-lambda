"""End-to-end tests for the Lambda handler.

These tests import handler.py (which loads all 4 ONNX models at module level)
and call handler() directly, simulating a real Lambda invocation.

Skipped locally (no onnxruntime). Runs in CodeBuild during deployment where
onnxruntime and models are installed — serves as the deployment gate.
"""

from __future__ import annotations

import base64
import io
import json
import os

import numpy as np
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from helpers import requires_models


def _make_image_b64(width: int = 400, height: int = 300, with_text: bool = True) -> str:
    """Create a base64-encoded JPEG test image with text-like dark regions."""
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    if with_text:
        draw = ImageDraw.Draw(img)
        # Draw dark horizontal bands to simulate text lines
        for y in range(50, height - 50, 40):
            draw.rectangle([30, y, width - 30, y + 15], fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _make_pdf_b64(num_pages: int = 2) -> str:
    """Create a base64-encoded PDF with blank pages."""
    pdf = pdfium.PdfDocument.new()
    for _ in range(num_pages):
        pdf.new_page(400, 300)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return base64.b64encode(buf.getvalue()).decode()


class FakeContext:
    """Minimal Lambda context for local testing."""
    aws_request_id: str = "handler-e2e-test"
    function_name: str = "ndl-ocr-lite"
    memory_limit_in_mb: int = 3008

    def get_remaining_time_in_millis(self) -> int:
        return 60000


@requires_models
class TestHandlerWithImage:
    """Test handler() with base64-encoded images."""

    @pytest.fixture(autouse=True)
    def _import_handler(self) -> None:
        """Import handler (loads models) once for the test class."""
        import handler as _handler
        self.handler = _handler.handler

    def test_single_image_returns_200(self) -> None:
        event = {"image": _make_image_b64()}
        result = self.handler(event, FakeContext())

        assert result["statusCode"] == 200
        assert "pages" in result["body"]
        assert len(result["body"]["pages"]) == 1

    def test_page_structure(self) -> None:
        event = {"image": _make_image_b64()}
        result = self.handler(event, FakeContext())

        page = result["body"]["pages"][0]
        assert page["page"] == 1
        assert "text" in page
        assert isinstance(page["text"], str)
        assert "imginfo" in page
        assert page["imginfo"]["img_width"] == 400
        assert page["imginfo"]["img_height"] == 300
        assert "img_path" not in page["imginfo"]
        assert "contents" in page
        assert isinstance(page["contents"], list)

    def test_contents_fields(self) -> None:
        event = {"image": _make_image_b64()}
        result = self.handler(event, FakeContext())

        for item in result["body"]["pages"][0]["contents"]:
            assert "boundingBox" in item
            assert len(item["boundingBox"]) == 4
            assert "id" in item
            assert "isVertical" in item
            assert "text" in item
            assert "isTextline" in item
            assert "confidence" in item

    def test_response_is_json_serializable(self) -> None:
        event = {"image": _make_image_b64()}
        result = self.handler(event, FakeContext())

        # Must be serializable — Lambda runtime does this
        serialized = json.dumps(result, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert deserialized["statusCode"] == 200

    def test_tmp_cleaned_up_after_invocation(self) -> None:
        event = {"image": _make_image_b64()}
        self.handler(event, FakeContext())

        work_dir = os.path.join("/tmp", FakeContext.aws_request_id)
        assert not os.path.exists(work_dir)


@requires_models
class TestHandlerWithPdf:
    """Test handler() with base64-encoded PDFs."""

    @pytest.fixture(autouse=True)
    def _import_handler(self) -> None:
        import handler as _handler
        self.handler = _handler.handler

    def test_pdf_returns_multiple_pages(self) -> None:
        event = {"image": _make_pdf_b64(num_pages=2)}
        result = self.handler(event, FakeContext())

        assert result["statusCode"] == 200
        assert len(result["body"]["pages"]) == 2
        assert result["body"]["pages"][0]["page"] == 1
        assert result["body"]["pages"][1]["page"] == 2

    def test_pdf_with_pages_param(self) -> None:
        event = {"image": _make_pdf_b64(num_pages=3), "pages": "1,3"}
        result = self.handler(event, FakeContext())

        assert result["statusCode"] == 200
        assert len(result["body"]["pages"]) == 2
        assert result["body"]["pages"][0]["page"] == 1
        assert result["body"]["pages"][1]["page"] == 2  # page numbering is sequential in output


@requires_models
class TestHandlerErrors:
    """Test handler() error paths."""

    @pytest.fixture(autouse=True)
    def _import_handler(self) -> None:
        import handler as _handler
        self.handler = _handler.handler

    def test_missing_image_returns_400(self) -> None:
        result = self.handler({}, FakeContext())
        assert result["statusCode"] == 400
        assert "error" in result["body"]
        assert "Missing required parameter" in result["body"]["error"]

    def test_invalid_base64_returns_400(self) -> None:
        result = self.handler({"image": "not-valid!!!"}, FakeContext())
        assert result["statusCode"] == 400
        assert "error" in result["body"]

    def test_empty_image_returns_400(self) -> None:
        result = self.handler({"image": ""}, FakeContext())
        assert result["statusCode"] == 400
