"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambda"
VENDOR_SRC = LAMBDA_DIR / "vendor" / "ndlocr-lite" / "src"


def _has_onnxruntime() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


requires_models = pytest.mark.skipif(
    not ((VENDOR_SRC / "model" / "deim-s-1024x1024.onnx").exists() and _has_onnxruntime()),
    reason="Skipped locally — runs in CodeBuild where onnxruntime and models are installed",
)
