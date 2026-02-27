# Proposal: Strengthen Tests to Prevent Post-Deployment Failures

## Summary

The current test suite (31 tests across 5 files) covers input parsing, output structure, and CDK resource definitions well. However, several critical code paths that execute **only in the deployed Lambda environment** have zero test coverage. This proposal identifies gaps ranked by deployment-failure risk and proposes concrete fixes.

## Analysis Method

Every line of `handler.py`, `input_parser.py`, `ocr_engine.py`, `pdf_utils.py`, the CDK stacks, and `deployments/buildspec.yml` was reviewed against the existing tests. Gaps were classified by whether they could cause a **runtime failure after deployment** (not just a missing feature test).

---

## Critical Gaps (Will Cause or Hide Post-Deployment Failures)

### C1. `handler()` function is never called in any test

**Risk:** The actual Lambda entry point — including its error handling branches, response assembly, and `/tmp` cleanup — is never exercised.

**Evidence:**
- `test_handler.py` tests `parse_input()` and `process_single_image()` separately, but never calls `handler(event, context)`.
- `TestHandlerOutputContract` asserts on a **hand-crafted dict literal**, not on actual handler output. This validates nothing about the real code.
- `TestHandlerTmpCleanup.test_cleanup_removes_directory` calls `shutil.rmtree` directly — it doesn't test that the handler's `finally` block runs.

**What could break undetected:**
- The `try/except ValueError → 400` and `except Exception → 500` branches (lines 110-120).
- The `/tmp` cleanup in the `finally` block (lines 121-124).
- Any regression in how `result["page"] = page_num` is assigned (line 102).

**Fix:** Add tests that call `handler()` with mocked models. Patch `detector`, `recognizer30/50/100` at module level and call `handler(event, fake_context)` to verify:
- 200 response with correct structure for valid input.
- 400 response for missing/invalid `image` parameter.
- 500 response when `process_single_image` raises.
- `/tmp/<request_id>/` directory is cleaned up even on error.

```python
# Sketch
@patch("handler.process_single_image", return_value=FAKE_PAGE_RESULT)
@patch("handler.parse_input", return_value=(["/tmp/test/page_001.jpg"], False))
def test_handler_200(mock_parse, mock_ocr, lambda_context):
    from handler import handler  # requires model-loading to be patchable
    resp = handler({"image": "abc"}, lambda_context)
    assert resp["statusCode"] == 200
    assert len(resp["body"]["pages"]) == 1
```

**Challenge:** `handler.py` loads models at module level (lines 32-62), so importing it in a test environment without models fails immediately. This is itself a testability problem — see C2.

---

### C2. Module-level model loading makes `handler.py` un-importable in CI

**Risk:** `handler.py` opens `NDLmoji.yaml` and loads 4 ONNX models at import time. In the CodeBuild CI environment (and local dev without `.sandbox/`), **importing handler.py crashes**, making it impossible to test the handler function even with mocks.

**Evidence:** `conftest.py` sets `NDLOCR_SRC_DIR` to `.sandbox/ndlocr-lite/src`, but `handler.py` overwrites it to `/opt/src` (line 27) and immediately opens `/opt/config/NDLmoji.yaml` (line 34). Tests work around this by never importing `handler`.

**What could break undetected:** Any bug in the handler function body, since it is structurally untestable.

**Fix:** Refactor module-level loading to be guarded or deferrable:

```python
# Option A: Guard with lazy initialization
_models_loaded = False
def _ensure_models():
    global _models_loaded, detector, recognizer30, ...
    if _models_loaded:
        return
    # ... load models ...
    _models_loaded = True

def handler(event, context):
    _ensure_models()
    ...
```

This still enables SnapStart (call `_ensure_models()` in an init hook or let the first snapshot invocation trigger it) while making the module importable in tests.

```python
# Option B: Move model loading to a separate module
# models.py — loaded at module level, snapshotted by SnapStart
# handler.py — imports from models.py, testable with mocks
```

---

### C3. S3 input path has zero test coverage

**Risk:** The S3 URI code path (`_is_s3_uri` → `_download_s3` → file read → PDF/image detection) is a **user-facing feature** (US-1 acceptance criteria: "Base64-encoded or S3 URI") with no unit test and no integration test.

**Evidence:**
- `_download_s3()` is never called in any test.
- `parse_input()` is never tested with an S3 URI input.
- `TestIsS3Uri` only tests the boolean detection function, not the download path.

**What could break undetected:**
- Incorrect S3 URI regex parsing (line 54).
- Missing IAM permissions for the Lambda execution role.
- File handling: the downloaded file is saved as `input_file` with no extension, then read back and passed to `_is_pdf()` / `_save_image()`.
- S3 client created at module level (`s3_client = boto3.client("s3")`, line 13) — no test verifies it initializes correctly.

**Fix:** Add unit tests with mocked boto3:

```python
@patch("input_parser.s3_client")
def test_s3_uri_image(mock_s3):
    mock_s3.download_file.side_effect = lambda b, k, p: _write_jpeg(p)
    with tempfile.TemporaryDirectory() as tmpdir:
        paths, is_pdf = parse_input({"image": "s3://bucket/key.jpg"}, tmpdir)
        assert len(paths) == 1
        assert is_pdf is False
        mock_s3.download_file.assert_called_once_with("bucket", "key.jpg", ANY)

@patch("input_parser.s3_client")
def test_s3_uri_pdf(mock_s3):
    mock_s3.download_file.side_effect = lambda b, k, p: _write_pdf(p)
    with tempfile.TemporaryDirectory() as tmpdir:
        paths, is_pdf = parse_input({"image": "s3://my-bucket/doc.pdf"}, tmpdir)
        assert is_pdf is True

@patch("input_parser.s3_client")
def test_s3_download_failure(mock_s3):
    mock_s3.download_file.side_effect = Exception("Access Denied")
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(Exception):
            parse_input({"image": "s3://bucket/key.jpg"}, tmpdir)
```

---

### C4. Lambda Layer packaging is not validated

**Risk:** `deployments/buildspec.yml` copies specific files from the NDL-OCR Lite repo by hardcoded path (e.g., `cp /tmp/ndlocr-lite/src/ocr.py`). If the upstream repo renames or reorganizes files, the build succeeds but the layer is **silently broken**.

**Evidence:**
- No test checks that the layer directory contains the expected files.
- No test validates that the handler's expected paths (`/opt/src/ocr.py`, `/opt/model/deim-s-1024x1024.onnx`, etc.) match what the buildspec creates.
- The CDK test `test_lambda_layer_created` only checks that a `AWS::Lambda::LayerVersion` resource exists — not its contents.

**What could break undetected:**
- Upstream renames `ocr.py` → `main.py`.
- Upstream adds a new required model file.
- Upstream changes the config file format.
- buildspec `cp` commands fail silently if source files don't exist (cp without `-f` exits non-zero, but if the buildspec continues...).

**Fix:** Add a buildspec validation step and a manifest test:

```yaml
# Add to buildspec.yml pre_build after file copy
- |
  for f in ocr.py deim.py parseq.py ndl_parser.py; do
    test -f layers/ocr-models/src/$f || { echo "MISSING: src/$f"; exit 1; }
  done
  for f in deim-s-1024x1024.onnx parseq-ndl-16x256-30-tiny-192epoch-tegaki3.onnx \
           parseq-ndl-16x384-50-tiny-146epoch-tegaki2.onnx \
           parseq-ndl-16x768-100-tiny-165epoch-tegaki2.onnx; do
    test -f layers/ocr-models/model/$f || { echo "MISSING: model/$f"; exit 1; }
  done
```

Also add a Python test that validates the handler's path assumptions match the layer structure:

```python
def test_handler_paths_match_layer_manifest():
    """Ensure handler expects the same files the buildspec packages."""
    EXPECTED_SRC = ["ocr.py", "deim.py", "parseq.py", "ndl_parser.py"]
    EXPECTED_MODELS = [
        "deim-s-1024x1024.onnx",
        "parseq-ndl-16x256-30-tiny-192epoch-tegaki3.onnx",
        "parseq-ndl-16x384-50-tiny-146epoch-tegaki2.onnx",
        "parseq-ndl-16x768-100-tiny-165epoch-tegaki2.onnx",
    ]
    EXPECTED_CONFIG = ["NDLmoji.yaml", "ndl.yaml"]
    # Parse handler.py source to extract referenced filenames and verify they match
    ...
```

---

### C5. CDK tests may be silently skipped in CI

**Risk:** `test_cdk_stacks.py` uses `@pytest.mark.skipif(not CDK_AVAILABLE)`. In the buildspec, `uv sync` runs in the `cdk/` directory, but `uv run pytest` runs from the project root. If `aws-cdk-lib` isn't in the root `pyproject.toml`'s dependencies (it's under `[project.optional-dependencies] cdk`), the CDK tests are **silently skipped** in CI — and the build still passes.

**Evidence:**
- `pyproject.toml` line 20: `cdk = ["aws-cdk-lib>=2.170.0", "constructs>=10.0.0"]`
- `buildspec.yml` line 11: `cd cdk && uv sync && cd ..`
- `buildspec.yml` line 32: `uv run pytest tests/ -v` (runs from root, not from cdk/)

**What could break undetected:** Any CDK stack misconfiguration (wrong memory, missing permissions, broken SnapStart config).

**Fix:** Either:
1. Install CDK deps for the root project: `uv sync --extra cdk` before running tests.
2. Or add a CI check that **no tests were skipped**: `uv run pytest tests/ -v --strict-markers` + require CDK marker.
3. Or at minimum, fail the build if CDK tests are skipped: add a `conftest.py` check that counts skipped CDK tests.

---

## High-Risk Gaps (Likely to Cause Issues Post-Deployment)

### H1. No test for corrupted or unsupported image input

**Risk:** If a user sends valid base64 that decodes to non-image data (e.g., a text file), `Image.open()` in `_save_image()` raises `UnidentifiedImageError`. The handler should return 400, but this error path is untested.

**Fix:**
```python
def test_non_image_base64_returns_error():
    b64 = base64.b64encode(b"this is not an image").decode()
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises((ValueError, Exception)):
            parse_input({"image": b64}, tmpdir)
```

Also consider: should `_save_image` catch PIL errors and raise `ValueError` so the handler returns 400 instead of 500?

### H2. `isVertical` is hardcoded to `"true"` for all content items

**Risk:** In `ocr_engine.py:158`, every content item has `"isVertical": "true"` regardless of actual text direction. This is likely a bug — the line-level `line_h > line_w` check (line 121) tracks vertical lines for page-level text reversal but is never used per-item.

**Evidence:** The spec says `isVertical` is per-content-item, and `tatelinecnt` is already computed per line, but the value is never assigned per item.

**Fix:** Use the per-line vertical detection:
```python
is_vertical = "true" if line_h > line_w else "false"
# in the JSON assembly loop, use is_vertical instead of hardcoded "true"
```

Add a test that processes a clearly horizontal image and verifies `isVertical` is `"false"`.

### H3. No test for non-JPEG image formats (PNG, WEBP, BMP, TIFF)

**Risk:** Users will send PNG screenshots, WEBP images, etc. `_save_image()` converts via PIL, but this path is untested for anything except JPEG.

**Fix:**
```python
@pytest.mark.parametrize("fmt", ["PNG", "BMP", "TIFF", "WEBP"])
def test_non_jpeg_image_converted(fmt):
    img = Image.new("RGB", (10, 10))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths, is_pdf = parse_input({"image": b64}, tmpdir)
        assert paths[0].endswith(".jpg")
        reopened = Image.open(paths[0])
        assert reopened.format == "JPEG"
```

### H4. No test for `parse_pages` with malformed input strings

**Risk:** `parse_pages("abc", 5)` raises `ValueError` from `int()`. `parse_pages("1-2-3", 5)` raises `ValueError` from split. These unhandled exceptions propagate as 500 errors instead of 400.

**Fix:** Either add input validation to `parse_pages()` that raises `ValueError` with a user-friendly message, or test that the handler's error handling catches these gracefully:

```python
@pytest.mark.parametrize("bad_input", ["abc", "1-2-3", "1,,2", "-1", "1.5"])
def test_malformed_pages_raises(bad_input):
    with pytest.raises(ValueError):
        parse_pages(bad_input, 5)
```

---

## Medium-Risk Gaps

### M1. CloudWatch alarm thresholds not validated in CDK tests

`test_cloudwatch_alarms_created` checks count (2 alarms) but not thresholds. If someone changes the error threshold from 5 to 500, tests still pass.

### M2. No Cognito OAuth scope validation

`TestGatewayStack` checks that Cognito resources exist but doesn't verify the OAuth scopes (`gateway:read`, `gateway:write`) or that the client uses `client_credentials` flow.

### M3. No test for Lambda environment variables

The CDK stack sets `LAMBDA_LAYER_DIR` and `NDLOCR_SRC_DIR` on the Lambda function, but no CDK test validates these values.

### M4. No concurrent `/tmp` cleanup test

Two warm invocations with overlapping timing could theoretically interfere if `request_id` generation has issues. Low risk since AWS guarantees unique request IDs, but a sanity test would be worthwhile.

---

## Recommended Implementation Priority

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| **P0** | C3 — S3 path unit tests (mocked boto3) | Small | Unblocks a primary user-facing feature |
| **P0** | C5 — Ensure CDK tests run in CI | Small | Prevents silent CDK test skipping |
| **P0** | C4 — Layer packaging validation in buildspec | Small | Catches upstream breaking changes |
| **P1** | C2 — Refactor handler for testability | Medium | Unblocks all handler-level tests |
| **P1** | C1 — Add handler() function tests | Medium | Validates error handling and cleanup |
| **P1** | H1 — Corrupted input test | Small | Prevents 500 errors for bad input |
| **P1** | H4 — Malformed page string tests | Small | Prevents 500 errors for bad pages param |
| **P2** | H2 — Fix `isVertical` hardcoding | Small | Correctness fix + test |
| **P2** | H3 — Non-JPEG format tests | Small | Validates common user input |
| **P2** | M1-M3 — CDK assertion improvements | Small | Catches config drift |

## Estimated Effort

- **P0 items:** ~2 hours (mostly test code, one buildspec change)
- **P1 items:** ~4 hours (handler refactor is the largest piece)
- **P2 items:** ~2 hours (straightforward test additions + one bug fix)
- **Total:** ~1 day of focused work

## Conclusion

The test suite is well-structured and covers input parsing and CDK resources thoroughly. However, three structural issues create blind spots for deployment failures:

1. **The handler function is untestable** due to module-level model loading.
2. **The S3 input path is completely untested.**
3. **CI may silently skip CDK tests.**

Fixing these three issues (C1-C3, C5) would significantly reduce the risk of post-deployment errors. The remaining items (H1-H4, M1-M4) are incremental improvements that further harden the suite.
