# Implementation Kick-off Prompt

Use the following prompt to start implementation. Copy everything below the line.

---

## Task

Implement the NDL-OCR Lite Lambda MCP service as specified in `spec/requirements.md`, `spec/design.md`, and `spec/implementation_qa.md`. The specs are fully reviewed and consistent — treat them as the source of truth. Follow `CLAUDE.md` for all development policies (use `uv`, never bare `python`, use `.sandbox/` for experimentation, etc.).

Nothing is built yet. The repository has only spec documents. You are building from scratch.

## What to build

There are 4 deliverables, in dependency order:

### 1. Lambda handler (`lambda/`)

The core OCR wrapper. This is a **thin wrapper** — do not reimplement OCR logic.

**Key files to create:**
- `lambda/handler.py` — Entry point. Module-level model loading + `handler(event, context)` function.
- `lambda/ocr_engine.py` — Extracted pipeline: wraps DEIM detection, XML/reading-order assembly, and PARSeq cascade recognition into reusable functions that accept pre-loaded model objects.
- `lambda/pdf_utils.py` — PDF-to-image rendering via pypdfium2.
- `lambda/input_parser.py` — Parses `event` dict: detects base64 vs S3 URI, decodes image, handles PDF detection, parses `pages` parameter (e.g. `"1-3"`, `"1,3,5"`).
- `lambda/pyproject.toml` — Dependencies (only the ones needed: onnxruntime, Pillow, numpy, PyYAML, lxml, networkx, pyparsing, ordered-set, pypdfium2). Exclude flet, reportlab, dill, tqdm.

**Critical design decisions (from implementation_qa.md):**
1. Do **NOT** call `process()`. It reloads all 4 ONNX models (~5s) on every invocation. Instead, load models at module level and call detection/recognition functions directly.
2. Module-level initialization loads 4 models: 1 DEIM detector + 3 PARSeq recognizers. SnapStart snapshots this state.
3. Per-invocation flow: parse input → (if PDF) render pages with pypdfium2 → for each image: `detector.detect()` → `convert_to_xml_string3()` + `eval_xml()` → `process_cascade(rec30, rec50, rec100)` → assemble JSON → strip `img_path` → return.
4. Use `/tmp/<request_id>/` subdirectories per invocation, clean up after. Simple filenames only (`page_001.jpg`) to avoid the dotted-filename bug.
5. Don't rely on exceptions from NDL-OCR Lite — verify output existence.
6. Response format: `{ "statusCode": 200, "body": { "pages": [...] } }` — see `spec/design.md` "Lambda returns structured response" for exact schema.

**To understand NDL-OCR Lite's internals**, clone it into `.sandbox/`:
```bash
cd .sandbox && git clone https://github.com/ndl-lab/ndlocr-lite.git
```
Read `src/ocr.py` (the `process()` function and surrounding code), `src/deim.py` (DEIM class), `src/parseq.py` (PARSEQ class), and `src/reading_order/` to understand how to extract the pipeline. The model files, `NDLmoji.yaml`, and `ndl.yaml` are in the repo's `models/` directory.

### 2. CDK infrastructure (`cdk/`)

Two stacks as designed in `spec/design.md`:

- **`OcrLambdaStack`** — Lambda function (Python 3.12, SnapStart enabled, 3008 MB memory, 60s timeout), Lambda Layer (models + deps), S3 bucket (24h lifecycle, SSE-S3, no public access), IAM roles, CloudWatch log group + alarms.
- **`GatewayStack`** — Cognito User Pool + resource server + app client, AgentCore Gateway with Lambda target and tool schema (`ocr_extract_text`), IAM role for Gateway→Lambda invocation.

Use `cdk/app.py` as the CDK app entry point. Stacks in `cdk/stacks/`. CDK is Python — use `uv` for running it.

### 3. One-click deployment (`deployments/`)

- `deployments/template.yaml` — CloudFormation bootstrap template (CodeBuild project, SNS topic, trigger Lambda, IAM roles). Parameters: `StackPrefix`, `NotificationEmail`, `LambdaMemoryMB`, `LambdaTimeoutSec`.
- `deployments/buildspec.yml` — CodeBuild phases: install (Node 18, Python 3.12, CDK), pre_build (package Lambda layer, run tests), build (`cdk deploy --all`), post_build (publish endpoint URL to SNS).

### 4. Tests (`tests/`)

- Unit tests for the Lambda handler (mock ONNX models, test input parsing, PDF rendering, error paths).
- Unit tests for CDK stacks (snapshot tests or assertion-based).
- All tests run with `uv run pytest`.

## Implementation order

1. **Start in `.sandbox/`**: Clone NDL-OCR Lite, read the source code, understand the pipeline extraction points. Prototype the extracted pipeline to verify it works before writing the Lambda handler.
2. **`lambda/`**: Build the handler with extracted pipeline. Test locally with `uv run pytest`.
3. **`cdk/`**: Build both CDK stacks. Verify with `cdk synth`.
4. **`deployments/`**: Create the bootstrap template and buildspec.
5. **`tests/`**: Add unit tests for all components.

## What NOT to do

- Do not reimplement OCR logic. Wrap NDL-OCR Lite's classes and functions.
- Do not call `process()` directly (it reloads models every time).
- Do not use container images for Lambda (SnapStart requires managed runtime).
- Do not use bare `python` — always `uv run python`.
- Do not commit `.sandbox/` contents.
- Do not add features not in the spec (no caching layer, no async processing, no multi-language support).
