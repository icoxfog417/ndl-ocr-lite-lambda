# Requirements

## Vision

**Allow your desktop agent to read anything you have.**

Users have scanned books, photographed documents, archived PDFs, and printed pages that contain valuable information locked in images. Today's AI agents can reason, summarize, and answer questions — but they cannot read scanned text accurately on their own. This project bridges that gap by giving any MCP-compatible agent access to a production-grade OCR engine, deployed in minutes with no infrastructure expertise.

## User Stories

### US-1: Extract text from a scanned image

> As an **end user** working with an AI agent,
> I want to **send an image of a scanned document to my agent and receive the extracted text**,
> so that I can **search, summarize, or ask questions about content that only exists as images**.

**Acceptance Criteria:**

- AC-1.1: The user can paste or attach a JPG, PNG, TIFF, JP2, or BMP image in their agent conversation
- AC-1.2: The agent calls the `ocr_extract_text` MCP tool and returns the recognized text
- AC-1.3: The extracted text preserves the original reading order of the document
- AC-1.4: Japanese text is recognized accurately (character error rate comparable to NDL-OCR Lite standalone)
- AC-1.5: The response is returned within 30 seconds for a typical single-page document image

### US-2: Understand document layout

> As an **end user** analyzing a complex document,
> I want to **receive layout information (headings, body text, captions) along with the extracted text**,
> so that I can **understand the structure of the document, not just its raw text**.

**Acceptance Criteria:**

- AC-2.1: When `include_layout` is set to `true`, the response includes bounding box regions with type labels
- AC-2.2: Region types include at minimum: `title`, `body`, `caption`, `header`, `footer`
- AC-2.3: Each region contains its extracted text and coordinates

### US-3: Process images stored in S3

> As an **end user** with a large document archive,
> I want to **point my agent at images already stored in S3**,
> so that I can **process existing archives without re-uploading files**.

**Acceptance Criteria:**

- AC-3.1: The `image` parameter accepts S3 URIs in the format `s3://bucket/key`
- AC-3.2: The Lambda function reads the image from S3 using its execution role
- AC-3.3: Access is limited to the designated S3 bucket created by the stack

### US-4: Deploy with one click

> As a **developer or team lead**,
> I want to **deploy the entire OCR service by launching a single CloudFormation stack**,
> so that I can **get the service running without installing CDK, Node.js, or any local tooling**.

**Acceptance Criteria:**

- AC-4.1: A single CloudFormation template can be launched from the AWS Console
- AC-4.2: The only required parameters are stack name and notification email
- AC-4.3: CodeBuild runs CDK to provision all resources (Lambda, S3, AgentCore Gateway)
- AC-4.4: An email notification is sent when deployment completes
- AC-4.5: The stack outputs include the MCP endpoint URL ready for agent configuration
- AC-4.6: Total deployment time is under 15 minutes

### US-5: Connect agent to MCP endpoint

> As a **developer**,
> I want to **add the MCP endpoint URL to my agent's configuration and immediately start using OCR**,
> so that I can **integrate OCR capability without writing any code**.

**Acceptance Criteria:**

- AC-5.1: The MCP endpoint URL is available in CloudFormation stack outputs
- AC-5.2: The endpoint is compatible with the MCP specification used by Claude Desktop, Cline, and other MCP clients
- AC-5.3: The agent can discover the `ocr_extract_text` tool via standard MCP tool listing
- AC-5.4: Authentication is handled via AgentCore Gateway's OAuth layer

### US-6: Deploy via CDK for customization

> As a **developer** who needs to customize the deployment,
> I want to **deploy using CDK directly from my local machine**,
> so that I can **modify stack parameters, add resources, or integrate with existing infrastructure**.

**Acceptance Criteria:**

- AC-6.1: `cdk deploy --all` provisions the complete stack
- AC-6.2: Stack parameters can be overridden via CDK context or environment variables
- AC-6.3: The CDK app is structured with separate stacks for Lambda/S3 and Gateway concerns

## Non-Functional Requirements

### NFR-1: Performance

- Single-page OCR latency: < 30 seconds (p95)
- Lambda memory: configurable, default 3008 MB (needed for ML model inference)
- Lambda timeout: 60 seconds
- Cold start: < 15 seconds (using provisioned concurrency or SnapStart if available)

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

- **Image size limit**: Lambda payload limit is 6 MB for synchronous invocations. Images larger than 6 MB must be uploaded to S3 first and referenced by URI.
- **CPU-only inference**: NDL-OCR Lite runs on CPU (ONNX Runtime). This is a deliberate trade-off for simplicity and cost over raw speed.
- **Japanese-focused**: NDL-OCR Lite is optimized for Japanese text. Recognition of other languages is not guaranteed.
- **Single-page processing**: Each invocation processes one image. Batch/multi-page processing is out of scope for v1.

## Out of Scope (v1)

- PDF input support (PDF-to-image conversion is a separate concern)
- Multi-page batch processing
- Real-time streaming of OCR results
- Fine-tuning or retraining of OCR models
- Multi-language OCR beyond Japanese
