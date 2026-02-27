"""AWS Lambda handler for NDL-OCR Lite.

Module-level initialization loads 4 ONNX models (1 DEIM + 3 PARSeq).
SnapStart snapshots this state so cold starts restore in <1s.
"""

from __future__ import annotations

import os
import shutil
import traceback
from typing import Any

from yaml import safe_load

# ---------------------------------------------------------------------------
# Module-level model loading (executed once, then snapshotted by SnapStart)
# ---------------------------------------------------------------------------

# On Lambda: source/models/config live in the Layer at /opt.
# Locally: fall back to the vendored submodule at lambda/vendor/ndlocr-lite/src.
_HANDLER_DIR = os.path.dirname(__file__)
_VENDOR_SRC = os.path.join(_HANDLER_DIR, "vendor", "ndlocr-lite", "src")

_LAYER_DIR = os.environ.get("LAMBDA_LAYER_DIR", "")
if _LAYER_DIR and os.path.isdir(os.path.join(_LAYER_DIR, "src")):
    # Running on Lambda — use layer paths
    _SRC_DIR = os.path.join(_LAYER_DIR, "src")
    _MODEL_DIR = os.path.join(_LAYER_DIR, "model")
    _CONFIG_DIR = os.path.join(_LAYER_DIR, "config")
else:
    # Running locally — use vendored submodule
    _SRC_DIR = _VENDOR_SRC
    _MODEL_DIR = os.path.join(_VENDOR_SRC, "model")
    _CONFIG_DIR = os.path.join(_VENDOR_SRC, "config")

os.environ["NDLOCR_SRC_DIR"] = _SRC_DIR

from ocr_engine import load_detector, load_recognizer, process_single_image
from input_parser import parse_input

# Load character vocabulary
_charlist_path = os.path.join(_CONFIG_DIR, "NDLmoji.yaml")
with open(_charlist_path, encoding="utf-8") as _f:
    _charobj = safe_load(_f)
_charlist: list[str] = list(_charobj["model"]["charset_train"])

# Load detector (DEIM)
detector = load_detector(
    model_path=os.path.join(_MODEL_DIR, "deim-s-1024x1024.onnx"),
    class_mapping_path=os.path.join(_CONFIG_DIR, "ndl.yaml"),
)

# Load 3 PARSeq recognizers
recognizer30 = load_recognizer(
    model_path=os.path.join(
        _MODEL_DIR, "parseq-ndl-16x256-30-tiny-192epoch-tegaki3.onnx"
    ),
    charlist=_charlist,
)
recognizer50 = load_recognizer(
    model_path=os.path.join(
        _MODEL_DIR, "parseq-ndl-16x384-50-tiny-146epoch-tegaki2.onnx"
    ),
    charlist=_charlist,
)
recognizer100 = load_recognizer(
    model_path=os.path.join(
        _MODEL_DIR, "parseq-ndl-16x768-100-tiny-165epoch-tegaki2.onnx"
    ),
    charlist=_charlist,
)


# ---------------------------------------------------------------------------
# Handler (called per invocation)
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point. Receives event from AgentCore Gateway.

    Event format:
        {
            "image": "<base64 or s3://...>",
            "pages": "1-3"  (optional, PDF only)
        }

    Returns:
        {
            "statusCode": 200,
            "body": { "pages": [...] }
        }
    """
    # Use request ID for unique /tmp subdirectory
    request_id: str = getattr(context, "aws_request_id", None) or "local"
    work_dir = os.path.join("/tmp", request_id)

    try:
        image_paths, is_pdf = parse_input(event, work_dir)

        pages: list[dict] = []
        for page_num, img_path in enumerate(image_paths, start=1):
            if not os.path.exists(img_path):
                raise RuntimeError(
                    f"Expected image file not found: {os.path.basename(img_path)}"
                )

            result = process_single_image(
                img_path, detector, recognizer30, recognizer50, recognizer100
            )
            result["page"] = page_num
            pages.append(result)

        return {
            "statusCode": 200,
            "body": {"pages": pages},
        }

    except ValueError as e:
        return {
            "statusCode": 400,
            "body": {"error": str(e)},
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "statusCode": 500,
            "body": {"error": f"Internal error: {type(e).__name__}: {e!s}"},
        }
    finally:
        # Clean up /tmp to prevent stale data on warm Lambda reuse
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
