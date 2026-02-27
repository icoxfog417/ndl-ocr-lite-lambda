"""Extracted NDL-OCR Lite pipeline: detection, reading order, and recognition.

This module wraps NDL-OCR Lite's DEIM detector, XML/reading-order assembly,
and PARSeq cascade recognizer into reusable functions that accept pre-loaded
model objects — avoiding the per-invocation model reload that process() does.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from typing import Any

import numpy as np
from PIL import Image

# NDL-OCR Lite source: Lambda Layer at /opt/src, or vendored submodule for local dev.
_SRC_DIR = os.environ.get(
    "NDLOCR_SRC_DIR",
    os.path.join(os.path.dirname(__file__), "vendor", "ndlocr-lite", "src"),
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# Increase recursion limit for XY-Cut reading order on complex layouts
sys.setrecursionlimit(5000)


def _import_ndlocr() -> tuple:
    """Lazy-import NDL-OCR Lite modules. Returns (DEIM, PARSEQ, RecogLine, process_cascade, convert_to_xml_string3, eval_xml)."""
    from deim import DEIM
    from parseq import PARSEQ
    from ocr import RecogLine, process_cascade
    from ndl_parser import convert_to_xml_string3
    from reading_order.xy_cut.eval import eval_xml

    return DEIM, PARSEQ, RecogLine, process_cascade, convert_to_xml_string3, eval_xml


def load_detector(
    model_path: str,
    class_mapping_path: str,
    *,
    score_threshold: float = 0.2,
    conf_threshold: float = 0.25,
    iou_threshold: float = 0.2,
    device: str = "cpu",
) -> Any:
    """Load the DEIM layout detector."""
    DEIM = _import_ndlocr()[0]
    return DEIM(
        model_path=model_path,
        class_mapping_path=class_mapping_path,
        score_threshold=score_threshold,
        conf_threshold=conf_threshold,
        iou_threshold=iou_threshold,
        device=device,
    )


def load_recognizer(model_path: str, charlist: list[str], device: str = "cpu") -> Any:
    """Load a PARSeq text recognizer."""
    PARSEQ = _import_ndlocr()[1]
    return PARSEQ(model_path=model_path, charlist=charlist, device=device)


def process_single_image(
    img_path: str,
    detector: Any,
    recognizer30: Any,
    recognizer50: Any,
    recognizer100: Any,
) -> dict:
    """Run the full OCR pipeline on a single image using pre-loaded models.

    Returns the per-page result dict with text, imginfo, and contents.
    """
    _, _, RecogLine, process_cascade, convert_to_xml_string3, eval_xml = _import_ndlocr()

    pil_image = Image.open(img_path).convert("RGB")
    img = np.array(pil_image)
    img_h, img_w = img.shape[:2]
    imgname = os.path.basename(img_path)

    # Step 1: Layout detection
    detections: list[dict] = detector.detect(img)
    classeslist: list[str] = list(detector.classes.values())

    # Step 2: Build detection result structure expected by convert_to_xml_string3
    resultobj: list[dict] = [dict(), dict()]
    resultobj[0][0] = list()
    for i in range(17):
        resultobj[1][i] = []
    for det in detections:
        xmin, ymin, xmax, ymax = det["box"]
        conf = det["confidence"]
        if det["class_index"] == 0:
            resultobj[0][0].append([xmin, ymin, xmax, ymax])
        resultobj[1][det["class_index"]].append([xmin, ymin, xmax, ymax, conf])

    # Step 3: XML assembly + reading order
    xmlstr = convert_to_xml_string3(img_w, img_h, imgname, classeslist, resultobj)
    xmlstr = "<OCRDATASET>" + xmlstr + "</OCRDATASET>"
    root = ET.fromstring(xmlstr)
    eval_xml(root, logger=None)

    # Step 4: Extract line images for recognition
    alllineobj: list = []
    tatelinecnt = 0
    alllinecnt = 0

    for idx, lineobj in enumerate(root.findall(".//LINE")):
        xmin = int(lineobj.get("X"))
        ymin = int(lineobj.get("Y"))
        line_w = int(lineobj.get("WIDTH"))
        line_h = int(lineobj.get("HEIGHT"))
        try:
            pred_char_cnt = float(lineobj.get("PRED_CHAR_CNT"))
        except (TypeError, ValueError):
            pred_char_cnt = 100.0

        if line_h > line_w:
            tatelinecnt += 1
        alllinecnt += 1

        lineimg = img[ymin : ymin + line_h, xmin : xmin + line_w, :]
        linerecogobj = RecogLine(lineimg, idx, pred_char_cnt)
        alllineobj.append(linerecogobj)

    # Step 5: Text recognition via cascade
    if alllineobj:
        resultlinesall: list[str] = process_cascade(
            alllineobj, recognizer30, recognizer50, recognizer100, is_cascade=True
        )
    else:
        resultlinesall = []

    # Step 6: Assemble JSON result
    resjsonarray: list[dict] = []
    for idx, lineobj in enumerate(root.findall(".//LINE")):
        xmin = int(lineobj.get("X"))
        ymin = int(lineobj.get("Y"))
        line_w = int(lineobj.get("WIDTH"))
        line_h = int(lineobj.get("HEIGHT"))
        try:
            conf = float(lineobj.get("CONF"))
        except (TypeError, ValueError):
            conf = 0

        text = resultlinesall[idx] if idx < len(resultlinesall) else ""
        jsonobj: dict = {
            "boundingBox": [
                [xmin, ymin],
                [xmin, ymin + line_h],
                [xmin + line_w, ymin],
                [xmin + line_w, ymin + line_h],
            ],
            "id": idx,
            "isVertical": "true",
            "text": text,
            "isTextline": "true",
            "confidence": conf,
        }
        resjsonarray.append(jsonobj)

    # Build full text (reverse for vertical-dominant pages, matching library behavior)
    alltextlist = ["\n".join(resultlinesall)]
    if alllinecnt > 0 and tatelinecnt / alllinecnt > 0.5:
        alltextlist = alltextlist[::-1]

    return {
        "text": "\n".join(alltextlist),
        "imginfo": {
            "img_width": img_w,
            "img_height": img_h,
        },
        "contents": resjsonarray,
    }
