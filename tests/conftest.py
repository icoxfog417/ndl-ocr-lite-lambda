"""Shared test fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add lambda/ to path so handler modules can be imported
LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambda"
VENDOR_SRC = LAMBDA_DIR / "vendor" / "ndlocr-lite" / "src"

sys.path.insert(0, str(LAMBDA_DIR))

# Point ocr_engine to the vendored NDL-OCR Lite source
os.environ.setdefault("NDLOCR_SRC_DIR", str(VENDOR_SRC))


@pytest.fixture
def lambda_context():
    """Create a fake Lambda context object."""

    class FakeContext:
        aws_request_id: str = "test-request-id-001"
        function_name: str = "ndl-ocr-lite"
        memory_limit_in_mb: int = 3008

        def get_remaining_time_in_millis(self) -> int:
            return 60000

    return FakeContext()
