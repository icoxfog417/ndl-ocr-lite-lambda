# Implementation Q&A

Findings from hands-on testing of NDL-OCR Lite v1.0.0 in `.sandbox/`. Each question targets a potential design or implementation bottleneck.

---

## Q1: How long does model loading take? (Cold start impact)

**Measured on dev machine (CPU):**

| Step | Time |
|------|------|
| Python imports (onnxruntime, PIL, etc.) | 0.72s |
| DEIM detector (39 MB) | 1.01s |
| PARSeq-30 recognizer (35 MB) | 0.53s |
| PARSeq-50 recognizer (36 MB) | 0.83s |
| PARSeq-100 recognizer (40 MB) | 2.06s |
| **Total model load** | **5.16s** |

**Bottleneck:** 5 seconds of model loading happens on **every call** to `process()`. See Q5 for why.

**Impact on Lambda:** Cold start = container init + model load (~5s). Warm start still pays model load cost because `process()` reloads models internally. This is the #1 performance bottleneck.

**Mitigation:** Do NOT call `process()` directly. Instead, load models once at module level (outside the handler) and call the detection/recognition functions directly. This way Lambda warm invocations skip the 5s model load entirely.

---

## Q2: How much memory does it use?

| Phase | Peak RSS |
|-------|----------|
| After loading all 4 models | 418 MB |
| After processing one image | 930 MB |

**Impact on Lambda:** The default 3008 MB in our design is sufficient, but 1024 MB would be too tight. 2048 MB is likely the minimum safe value. The ~930 MB peak is for a single 2048x1446 image; larger images will use more.

---

## Q3: How fast is inference once models are loaded?

**Warm inference (models already in memory), single page (2048x1446, 26 text lines):**

| Step | Time |
|------|------|
| Layout detection (DEIM) | 0.93s |
| XML parse + reading order | 0.08s |
| Text recognition (PARSeq cascade) | 0.57s |
| **Total** | **1.59s** |

**Batch (3 images via `--sourcedir`, includes per-image detector reload):**

| Image | Lines | Time |
|-------|-------|------|
| 827x1170, 77 lines | 77 | 2.58s |
| 2048x1446, 26 lines | 26 | 1.94s |
| 2048x1366, 144 lines | 144 | 3.05s |
| **Total (3 images)** | | **12.73s** |

The 12.73s includes ~5s of initial model loading + per-image DEIM reloads. With model caching, 3 images would take ~6s.

**Impact on Lambda:** Well within 60s timeout for single pages. Multi-page PDFs (5+ pages) need model caching to stay within timeout.

---

## Q4: What do the actual model files weigh?

| Model | File | Size |
|-------|------|------|
| DEIM (layout detection) | `deim-s-1024x1024.onnx` | 39 MB |
| PARSeq-30 (short lines) | `parseq-ndl-16x256-30-tiny-192epoch-tegaki3.onnx` | 35 MB |
| PARSeq-50 (medium lines) | `parseq-ndl-16x384-50-tiny-146epoch-tegaki2.onnx` | 36 MB |
| PARSeq-100 (long lines) | `parseq-ndl-16x768-100-tiny-165epoch-tegaki2.onnx` | 40 MB |
| **Total models** | | **150 MB** |

Plus config files: `NDLmoji.yaml` (42 KB), `ndl.yaml` (299 B).

**Impact on design:** 150 MB total is much smaller than the 500 MB+ estimate in the original design. This fits within Lambda's 250 MB zip limit if we exclude unnecessary dependencies. **Container image may not be strictly required** — a Lambda layer + zip deployment could work. However, container image is still recommended for reproducibility and to bundle Python + all deps cleanly.

---

## Q5: `process()` reloads models every call — this is the critical bottleneck

Looking at `ocr.py`:

```python
def process(args):
    # Lines 157-159: Recognizers loaded at start of every call
    recognizer100 = get_recognizer(args=args)
    recognizer30 = get_recognizer(args=args, weights_path=args.rec_weights30)
    recognizer50 = get_recognizer(args=args, weights_path=args.rec_weights50)

    for inputpath in inputpathlist:
        # Line 172: Detector ALSO reloaded per image via inference_on_detector()
        detections, classeslist = inference_on_detector(args=args, ...)
```

**Every call** to `process()` creates 4 new ONNX sessions (3 recognizers + 1 detector per image). On Lambda, this means:
- Warm invocation: Still pays ~5s model load
- Batch via `sourcedir`: Recognizers loaded once, but detector reloaded per image

**Decision required:** We must NOT call `process()` as-is from the Lambda handler. Instead, we should:

1. Load models at module level (Lambda global scope, persists across warm invocations)
2. Call `detector.detect()`, `process_cascade()`, and the XML/JSON assembly directly
3. This turns a 7s invocation (5s load + 2s inference) into a 2s invocation

This is the most important implementation decision for the Lambda wrapper.

---

## Q6: How does PDF rendering work?

**pypdfium2 (already bundled in NDL-OCR Lite dependencies):**

```python
import pypdfium2 as pdfium
pdf = pdfium.PdfDocument(pdf_path)
for i in range(len(pdf)):
    bitmap = pdf[i].render(scale=300/72)  # 300 DPI
    pil_img = bitmap.to_pil()
```

**Measured:** 2-page PDF rendered in 0.32s total (0.16s/page). Negligible cost compared to OCR.

**Important:** `process()` does NOT handle PDFs natively — it only accepts image files (jpg, png, tiff, jp2, bmp). The Lambda handler must:
1. Detect PDF input
2. Render pages to images with pypdfium2
3. Save as JPG files in `/tmp/input/`
4. Pass directory to `process()` (or call the pipeline directly per Q5)

---

## Q7: What does the actual JSON output look like?

```json
{
  "contents": [
    [
      {
        "boundingBox": [[380,229],[380,251],[569,229],[569,251]],
        "id": 0,
        "isVertical": "true",
        "text": "(z)気送子送付管",
        "isTextline": "true",
        "confidence": 0.895
      }
    ]
  ],
  "imginfo": {
    "img_width": 2048,
    "img_height": 1446,
    "img_path": "/path/to/input.jpg",
    "img_name": "input.jpg"
  }
}
```

**Key observations:**
- `contents` is a nested array: `contents[0]` is the list of text lines
- `boundingBox` is 4 corners: `[[x1,y1], [x1,y2], [x2,y1], [x2,y2]]`
- `isVertical` is hardcoded to `"true"` for every line (bug/limitation in ocr.py line 226) — page-level vertical detection uses `tatelinecnt/alllinecnt` ratio instead
- `img_path` contains the full local filesystem path — **must be stripped** before returning from Lambda
- `confidence` is 0 for some lines (e.g., page numbers)

---

## Q8: What about `/tmp` storage on Lambda?

| Item | Size |
|------|------|
| Input image (typical) | 100-300 KB |
| Output JSON | 12 KB |
| Output XML | 6 KB |
| Output TXT | 1.3 KB |
| **Per-image total** | **~320 KB** |

Lambda default `/tmp` is 512 MB. Even a 20-page PDF (20 input images + 20 output sets) would use ~6 MB. **Not a bottleneck.**

However, the Lambda handler must clean `/tmp` between invocations (warm Lambdas reuse the same `/tmp`). Use unique subdirectories per invocation.

---

## Q9: Which dependencies can be stripped for Lambda?

From `pyproject.toml` dependencies:

| Dependency | Needed for OCR? | Size | Notes |
|------------|-----------------|------|-------|
| onnxruntime==1.23.2 | Yes | ~50 MB | Core ML runtime |
| pillow==12.1.1 | Yes | ~7 MB | Image I/O |
| numpy==2.2.2 | Yes | ~16 MB | Array operations |
| PyYAML==6.0.1 | Yes | ~1 MB | Config loading |
| lxml==5.4.0 | Yes | ~5 MB | XML parsing |
| networkx==3.3 | Yes | ~2 MB | Reading order graph |
| pyparsing==3.1.2 | Yes | tiny | XML parsing support |
| ordered-set==4.1.0 | Yes | tiny | Used in reading order |
| pypdfium2==4.30.0 | Yes (PDF) | ~3 MB | PDF page rendering |
| protobuf==6.31.1 | Maybe | ~1 MB | ONNX Runtime dependency |
| **flet==0.27.6** | **No** | **~20 MB** | GUI framework |
| **reportlab==4.2.5** | **No** | **~2 MB** | PDF generation (we only read) |
| **dill==0.3.8** | **No** | tiny | Flet dependency |
| **tqdm==4.66.4** | **No** | tiny | Progress bars |
| sympy | No (transitive) | ~6 MB | onnxruntime optional |

**Strippable:** flet, reportlab, dill, tqdm, sympy → saves ~28 MB.

---

## Q10: Output file naming breaks on dotted filenames

`ocr.py` line 232-246 uses `os.path.basename(inputpath).split(".")[0]` to derive output filenames.

- `page_1.jpg` → `page_1.json` (OK)
- `scan.2024.01.jpg` → `scan.json` (WRONG — loses everything after first dot)

**Impact on Lambda:** When writing temp image files for `process()`, use simple filenames without extra dots: `page_001.jpg`, `page_002.jpg`, etc.

---

## Q11: The `process()` function silently returns on errors

```python
if len(inputpathlist) == 0:
    print("Images are not found.")
    return                         # silent return, no exception
if not os.path.exists(args.output):
    print("Output Directory is not found.")
    return                         # silent return, no exception
```

**Impact on Lambda:** The handler cannot rely on exceptions for error detection. Must check that output files actually exist after calling `process()` / the pipeline functions.

---

## Summary: Critical implementation decisions

| # | Decision | Recommendation |
|---|----------|----------------|
| 1 | **Call `process()` directly or extract pipeline?** | Extract pipeline. Load models at module level, call detection/recognition directly. Saves ~5s per warm invocation. |
| 2 | **Container image or zip deployment?** | Container image. Models are 150 MB (fits in zip), but container gives reproducibility + simpler bundling of ONNX Runtime + system libs. |
| 3 | **Memory allocation?** | 2048 MB minimum, 3008 MB recommended. Peak RSS is ~930 MB for a single page. |
| 4 | **PDF handling?** | Lambda handler renders pages with pypdfium2, saves as JPG to `/tmp`, feeds to pipeline. |
| 5 | **Error handling?** | Don't rely on exceptions from OCR library. Verify output files exist. |
| 6 | **Temp file management?** | Use unique `/tmp/<invocation_id>/input/` and `/tmp/<invocation_id>/output/` per invocation. Clean filenames (no dots except extension). |
| 7 | **Strip unnecessary deps?** | Remove flet, reportlab, dill to save ~22 MB in container. |
