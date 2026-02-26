# Requirements

## Vision

**Allow your desktop agent to read anything you have.**

Users have scanned books, photographed documents, archived PDFs, and printed pages that contain valuable information locked in images. Today's AI agents can reason, summarize, and answer questions — but they cannot read scanned text accurately on their own. This project bridges that gap by giving any MCP-compatible agent access to [NDL-OCR Lite](https://github.com/ndl-lab/ndlocr-lite), deployed in minutes with no infrastructure expertise.

## What NDL-OCR Lite already provides

NDL-OCR Lite is a complete OCR pipeline developed by Japan's National Diet Library. The library handles:

- **Layout recognition** — DEIMv2 detects 18 region classes (body, headings, captions, tables, etc.)
- **Character recognition** — PARSeq cascade (3 models of increasing capacity) with thread-pool parallelism
- **Reading order** — XY-Cut algorithm sequences detected regions into logical reading order
- **Structured output** — JSON with per-line bounding boxes, text, confidence, and vertical/horizontal detection
- **Image formats** — JPG, PNG, TIFF, JP2, BMP
- **PDF rendering** — pypdfium2 is already a bundled dependency

This project does **not** reimplement any OCR logic. Our job is the transport layer: receive input, pass it to `process()`, return the output.

## User Stories

### US-1: OCR an image via AI agent

> As an **end user** working with an AI agent,
> I want to **send an image of a document and receive the extracted text**,
> so that I can **search, summarize, or ask questions about content that only exists as images**.

**Acceptance Criteria:**

- AC-1.1: The `image` parameter accepts base64-encoded image data (JPG, PNG, TIFF, JP2, BMP) or an S3 URI (`s3://bucket/key`)
- AC-1.2: The Lambda writes the image to `/tmp`, calls NDL-OCR Lite's `process()`, and returns the JSON output
- AC-1.3: The response includes per-line text, bounding boxes, confidence scores, and image dimensions (NDL-OCR Lite's native JSON format)
- AC-1.4: OCR accuracy is identical to running NDL-OCR Lite standalone (no quality degradation from the Lambda wrapper)
- AC-1.5: Single-page response is returned within 10 seconds (p95, warm invocation ~2s)

### US-2: OCR a PDF via AI agent

> As an **end user** with a multi-page PDF,
> I want to **send a PDF and receive the extracted text for each page**,
> so that I can **work with scanned books and multi-page documents without manual page splitting**.

**Acceptance Criteria:**

- AC-2.1: The `image` parameter accepts base64-encoded PDF data or an S3 URI pointing to a PDF
- AC-2.2: The Lambda splits the PDF into page images using `pypdfium2` (already bundled in NDL-OCR Lite)
- AC-2.3: Each page image is passed to NDL-OCR Lite's `process()` individually
- AC-2.4: The `pages` parameter allows selecting a page range (e.g. `1-3`, `1,3,5`); default is all pages
- AC-2.5: The response contains a `pages` array with one entry per processed page

### US-3: Deploy with one click

> As a **developer or team lead**,
> I want to **deploy the entire OCR service by launching a single CloudFormation stack**,
> so that I can **get the service running without installing CDK, Node.js, or any local tooling**.

**Acceptance Criteria:**

- AC-3.1: A single CloudFormation template can be launched from the AWS Console
- AC-3.2: The only required parameters are stack name and notification email
- AC-3.3: CodeBuild runs CDK to provision all resources (Lambda, S3, AgentCore Gateway, Cognito)
- AC-3.4: An email notification is sent when deployment completes
- AC-3.5: The stack outputs include the MCP endpoint URL ready for agent configuration
- AC-3.6: Total deployment time is under 15 minutes

### US-4: Connect agent to MCP endpoint

> As a **developer**,
> I want to **add the MCP endpoint URL to my agent's configuration and immediately start using OCR**,
> so that I can **integrate OCR capability without writing any code**.

**Acceptance Criteria:**

- AC-4.1: The MCP endpoint URL is available in CloudFormation stack outputs
- AC-4.2: The endpoint is compatible with the MCP specification used by Claude Desktop, Cline, and other MCP clients
- AC-4.3: The agent can discover the `ocr_extract_text` tool via standard MCP tool listing
- AC-4.4: Authentication is handled via AgentCore Gateway's Cognito OAuth layer

### US-5: Deploy via CDK for customization

> As a **developer** who needs to customize the deployment,
> I want to **deploy using CDK directly from my local machine**,
> so that I can **modify stack parameters, add resources, or integrate with existing infrastructure**.

**Acceptance Criteria:**

- AC-5.1: `cdk deploy --all` provisions the complete stack
- AC-5.2: Stack parameters can be overridden via CDK context or environment variables
- AC-5.3: The CDK app is structured with separate stacks for Lambda/S3 and Gateway concerns

## Non-Functional Requirements

### NFR-1: Performance

Measured on dev machine (CPU). Lambda times may differ but relative proportions hold.

- Warm invocation (models cached): ~2s per page (detection 0.9s + recognition 0.6s + overhead)
- Cold start with SnapStart: ~2-3s for first page (models restored from snapshot in <1s + 1.6s inference)
- Cold start without SnapStart: ~7s for first page (5.2s model load + 1.6s inference)
- PDF page rendering: ~0.16s/page via pypdfium2 (negligible)
- Single-page OCR latency target: < 5 seconds (p95, including SnapStart cold starts)
- Lambda memory: 3008 MB (peak RSS measured at 930 MB; headroom for large images)
- Lambda timeout: 60 seconds (allows ~25 pages per invocation)
- SnapStart: Enabled by default (Python 3.12 managed runtime). Snapshots initialized ONNX models at publish time, eliminating the ~5s model load penalty on cold starts.

### NFR-2: Scalability

- Lambda concurrency scales automatically with demand
- AgentCore Gateway handles routing and load distribution
- S3 provides unlimited image storage

### NFR-3: Security

- All traffic encrypted in transit (TLS)
- AgentCore Gateway enforces OAuth-based authentication
- Lambda execution role follows least-privilege principle (read-only S3 access to designated bucket)
- No image data is persisted beyond the processing request unless stored in user's S3 bucket
- OCR model weights are bundled in the Lambda deployment package (no external model downloads at runtime)

### NFR-4: Cost Efficiency

- Serverless architecture means zero cost when idle
- No GPU instances required (NDL-OCR Lite runs on CPU with ONNX Runtime)
- Lambda is billed per-invocation with millisecond granularity

### NFR-5: Observability

- Lambda invocations are logged to CloudWatch Logs
- AgentCore Gateway provides built-in request tracing
- CloudWatch metrics for invocation count, duration, and errors
- Alarms for error rate threshold breach

### NFR-6: Maintainability

- Infrastructure as Code via AWS CDK (Python)
- Automated testing (unit + integration)
- NDL-OCR Lite model updates can be applied by rebuilding the Lambda package

## Constraints

- **Payload size limit**: Lambda payload limit is 6 MB for synchronous invocations. Images/PDFs larger than 6 MB must be uploaded to S3 first and referenced by URI.
- **CPU-only inference**: NDL-OCR Lite runs on CPU (ONNX Runtime). This is a deliberate trade-off for simplicity and cost over raw speed.
- **Japanese-focused**: NDL-OCR Lite is optimized for Japanese text. Recognition of other languages is not guaranteed.
- **Lambda timeout**: 60-second timeout limits the number of PDF pages processable in a single invocation. Large PDFs should use the `pages` parameter to process in batches.
- **Managed runtime (zip deployment)**: SnapStart requires Python 3.12 managed runtime — container images are not supported. Model weights (150 MB) fit within Lambda's 250 MB unzipped limit. Container image mode is available as an alternative (with Provisioned Concurrency instead of SnapStart).
- **Thin wrapper only**: This project does not modify, extend, or re-implement any NDL-OCR Lite logic. If the library has a limitation, so does this service.

## Out of Scope (v1)

- Real-time streaming of OCR results
- Fine-tuning or retraining of OCR models
- Multi-language OCR beyond Japanese
- Asynchronous / background processing of large PDF batches
- Custom post-processing of OCR output (e.g. spell correction, format conversion)
