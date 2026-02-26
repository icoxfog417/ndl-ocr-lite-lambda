# AWS Architecture Design

## System Context

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           End User's Machine                            │
│                                                                         │
│  ┌─────────────┐                                                        │
│  │  AI Agent   │  (Claude Desktop, Cline, custom agent, etc.)           │
│  │             │                                                        │
│  └──────┬──────┘                                                        │
│         │ MCP protocol (SSE/HTTP)                                       │
└─────────┼────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              AWS Cloud                                  │
│                                                                         │
│  ┌─────────────────────────────────────────┐                            │
│  │       Amazon Bedrock AgentCore          │                            │
│  │              Gateway                    │                            │
│  │                                         │                            │
│  │  - MCP endpoint (tool discovery/call)   │                            │
│  │  - OAuth authentication                 │                            │
│  │  - Request translation                  │                            │
│  │  - Auto-scaling                         │                            │
│  └──────────────────┬──────────────────────┘                            │
│                     │ invoke                                            │
│                     ▼                                                   │
│  ┌─────────────────────────────────────────┐    ┌────────────────────┐  │
│  │         AWS Lambda Function             │    │    Amazon S3       │  │
│  │         (OCR Processing)                │◄──►│    Bucket          │  │
│  │                                         │    │                    │  │
│  │  ┌───────────────────────────────────┐  │    │  - Image storage   │  │
│  │  │  NDL-OCR Lite Engine              │  │    │  - User uploads    │  │
│  │  │                                   │  │    └────────────────────┘  │
│  │  │  1. Layout Recognition (DEIMv2)   │  │                            │
│  │  │  2. Character Recognition(PARSeq) │  │    ┌────────────────────┐  │
│  │  │  3. Reading Order Sequencing      │  │    │  CloudWatch        │  │
│  │  │                                   │  │    │                    │  │
│  │  │  Runtime: ONNX (CPU)              │  │    │  - Logs            │  │
│  │  └───────────────────────────────────┘  │───►│  - Metrics         │  │
│  │                                         │    │  - Alarms          │  │
│  └─────────────────────────────────────────┘    └────────────────────┘  │
│                                                                         │
└──────────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Amazon Bedrock AgentCore Gateway

**Role:** MCP endpoint that bridges AI agents to the OCR Lambda function.

**Responsibilities:**
- Expose a single MCP-compatible endpoint for tool discovery and invocation
- Authenticate requests via OAuth
- Translate MCP `tools/call` requests into Lambda invocations
- Return Lambda responses as MCP tool results
- Auto-scale with incoming request volume

**Configuration:**
- Tool name: `ocr_extract_text`
- Tool description: Describes the OCR capability for agent tool selection
- Target: Lambda function ARN
- Auth: OAuth 2.0 via Amazon Cognito (User Pool with M2M client credentials)
- Credential provider: `GATEWAY_IAM_ROLE` for Lambda invocation

**Gateway Target Schema (Lambda):**

```json
{
  "mcp": {
    "lambda": {
      "lambdaArn": "arn:aws:lambda:<region>:<account>:function:ndl-ocr-lite",
      "toolSchema": {
        "inlinePayload": [
          {
            "name": "ocr_extract_text",
            "description": "Extract text from an image or PDF using NDL-OCR Lite. Supports Japanese text. Returns per-line text with bounding boxes and confidence scores.",
            "inputSchema": {
              "type": "object",
              "properties": {
                "image": {
                  "type": "string",
                  "description": "Base64-encoded image/PDF data or S3 URI (s3://bucket/key). Supports JPG, PNG, TIFF, JP2, BMP, and PDF."
                },
                "pages": {
                  "type": "string",
                  "description": "Page range for PDFs (e.g. '1-3', '1,3,5'). Default: all pages. Ignored for images."
                }
              },
              "required": ["image"]
            }
          }
        ]
      }
    }
  }
}
```

**Important:** The Lambda function accepts `Map<String, String>` parameters (not `APIGatewayProxyRequestEvent`), as AgentCore Gateway does not populate path parameters.

### 2. AWS Lambda Function (Pipeline Extraction with Model Caching)

**Role:** Loads NDL-OCR Lite models once, reuses them across warm invocations.

**Critical design decision:** NDL-OCR Lite's `process()` function reloads all 4 ONNX models (~5s) on every call. We do **not** call `process()` directly. Instead, we extract the pipeline components and cache models at module level (Lambda global scope), reducing warm invocation time from ~7s to ~2s.

See [implementation_qa.md](implementation_qa.md) Q1/Q5 for measured data behind this decision.

**Runtime Configuration:**
- Runtime: Python 3.10
- Memory: 3008 MB (peak RSS measured at 930 MB for single page; larger images need headroom)
- Timeout: 60 seconds
- Ephemeral storage: 512 MB (default, sufficient — see /tmp analysis below)
- Architecture: x86_64
- Packaging: Container image

**Handler Architecture:**

```python
# --- Module level (loaded once on cold start, persisted across warm invocations) ---

from deim import DEIM
from parseq import PARSEQ
# ... load config, charlist ...

detector    = DEIM(model_path=..., class_mapping_path=..., ...)        # 1.0s
recognizer30  = PARSEQ(model_path=..., charlist=charlist, device="cpu") # 0.5s
recognizer50  = PARSEQ(model_path=..., charlist=charlist, device="cpu") # 0.8s
recognizer100 = PARSEQ(model_path=..., charlist=charlist, device="cpu") # 2.1s
# Total cold start model load: ~5.2s (one-time cost)

# --- Handler (called per invocation) ---

def handler(event, context):
    # 1. Parse input (base64/S3 URI, detect PDF vs image)
    # 2. If PDF: render pages to images via pypdfium2 (~0.16s/page)
    # 3. For each image:
    #    a. detector.detect(img)                          # ~0.9s (reuses cached model)
    #    b. convert_to_xml_string3() + eval_xml()         # ~0.08s
    #    c. process_cascade(lines, rec30, rec50, rec100)  # ~0.6s
    #    d. Assemble JSON result
    # 4. Return { pages: [...] }
```

**Processing Flow:**

```
                    Cold start (first invocation only)
                    ┌────────────────────────────┐
                    │  Load 4 ONNX models (~5s)  │
                    │  Persisted in memory for    │
                    │  all subsequent invocations │
                    └─────────────┬──────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │          Per invocation (~2s warm)                 │
        │                                                   │
        │  Request received                                 │
        │       │                                           │
        │       ├── base64 image ──► decode to numpy array  │
        │       ├── base64 PDF ────► pypdfium2 render pages │
        │       ├── S3 URI ────────► boto3 download         │
        │       │                                           │
        │       ▼                                           │
        │  For each image:                                  │
        │    detector.detect(img)         [0.9s]            │
        │       │                                           │
        │       ▼                                           │
        │    convert_to_xml_string3()                       │
        │    eval_xml() (reading order)   [0.08s]           │
        │       │                                           │
        │       ▼                                           │
        │    process_cascade()            [0.6s]            │
        │    (rec30 → rec50 → rec100)                       │
        │       │                                           │
        │       ▼                                           │
        │    Assemble per-page JSON                         │
        │    Strip img_path (local path leak)               │
        │                                                   │
        │       ▼                                           │
        │    Return { pages: [...] }                        │
        └───────────────────────────────────────────────────┘
```

**Measured Performance (dev machine, CPU):**

| Scenario | Model load | Inference | Total |
|----------|-----------|-----------|-------|
| Cold start, 1 page | 5.2s | 1.6s | **~7s** |
| Warm invocation, 1 page | 0s | 1.6s | **~2s** |
| Warm invocation, 3 pages | 0s | ~5s | **~5s** |

**NDL-OCR Lite Pipeline (called per image, using cached models):**

1. **Layout detection** — `detector.detect(img)` using cached DEIM session. Detects 17 region classes (see `ndl.yaml`: text_block, line_main, line_caption, line_ad, line_note, block_fig, block_table, line_title, etc.).
2. **XML assembly + reading order** — `convert_to_xml_string3()` builds XML from detections, `eval_xml()` applies XY-Cut recursive bisection to assign reading order.
3. **Text recognition** — `process_cascade()` routes line images through 3 cached PARSeq models:
   - `rec30` (256px) → tries first, escalates if result >= 25 chars
   - `rec50` (384px) → escalates if result >= 45 chars
   - `rec100` (768px) → terminal model
   - Each tier uses `ThreadPoolExecutor` for parallel recognition

**Lambda Container Image:**

The container image bundles:
- Python 3.10 runtime
- NDL-OCR Lite source code (`ocr.py`, `deim.py`, `parseq.py`, `ndl_parser.py`, `reading_order/`)
- Required dependencies: onnxruntime 1.23.2, Pillow, NumPy, lxml, networkx, PyYAML, pypdfium2, pyparsing, ordered-set
- **Excluded** (GUI/unnecessary): flet, reportlab, dill, tqdm (~28 MB saved)
- ONNX model weights (150 MB total):
  - `deim-s-1024x1024.onnx` — 39 MB (layout detection)
  - `parseq-ndl-16x256-30-tiny-192epoch-tegaki3.onnx` — 35 MB
  - `parseq-ndl-16x384-50-tiny-146epoch-tegaki2.onnx` — 36 MB
  - `parseq-ndl-16x768-100-tiny-165epoch-tegaki2.onnx` — 40 MB
- Config: `NDLmoji.yaml` (42 KB character vocabulary), `ndl.yaml` (17 detection classes)

**Why container image over zip:**
Model weights total 150 MB (not 500 MB+ as initially estimated), which could fit in a zip deployment. However, container image is still preferred because: (1) reproducible build with exact system libraries for ONNX Runtime, (2) simpler Dockerfile than managing Lambda layers, (3) headroom for future model updates.

**`/tmp` Storage:**

Per-image I/O is minimal (~320 KB: input image + JSON/XML/TXT output). Lambda's default 512 MB `/tmp` handles 20+ pages easily. The handler uses unique subdirectories per invocation (`/tmp/<request_id>/`) to prevent stale data from warm Lambda reuse, and cleans up after each invocation. Temp filenames use simple `page_001.jpg` format to avoid the library's dotted-filename bug (`split(".")[0]`).

### 3. Amazon S3 Bucket

**Role:** Stores images that exceed the Lambda 6 MB payload limit.

**Configuration:**
- Bucket name: Auto-generated with stack prefix
- Encryption: SSE-S3 (AES-256)
- Versioning: Disabled (images are ephemeral processing inputs)
- Lifecycle: Objects auto-deleted after 24 hours
- Access: Lambda execution role has read-only access
- Public access: Blocked entirely

### 4. Amazon CloudWatch

**Role:** Observability for the OCR service.

**Resources:**
- **Log Group:** `/aws/lambda/ndl-ocr-lite` — Lambda execution logs
- **Metrics:** Invocation count, duration (p50/p95/p99), error count, throttle count
- **Alarms:**
  - Error rate > 5% over 5 minutes
  - p95 duration > 30 seconds
  - Concurrent executions > 80% of account limit

## One-Click Deployment Architecture

The one-click deployment uses a three-layer bootstrap pattern adapted from [sample-one-click-generative-ai-solutions](https://github.com/aws-samples/sample-one-click-generative-ai-solutions):

```
User clicks               CloudFormation             CodeBuild              CDK
"Launch Stack"            creates resources          runs build             deploys app
     │                         │                        │                      │
     ▼                         ▼                        ▼                      ▼
┌──────────┐  creates  ┌──────────────┐  triggers  ┌──────────┐  executes  ┌──────────┐
│  AWS     │─────────►│  Bootstrap    │──────────►│ CodeBuild │──────────►│  CDK     │
│  Console │          │  Stack        │           │  Project  │           │  Deploy  │
└──────────┘          │              │           │           │           │          │
                      │  - CodeBuild │           │  1. npm i │           │  - Lambda│
                      │  - SNS Topic │           │  2. cdk   │           │  - S3    │
                      │  - IAM Roles │           │     synth │           │  - GW    │
                      │  - Lambda    │           │  3. cdk   │           │          │
                      │    (trigger) │           │     deploy│           │          │
                      └──────────────┘           └──────────┘           └──────────┘
                                                       │
                                                       ▼
                                                 ┌──────────┐
                                                 │   SNS    │
                                                 │  Email   │
                                                 │  "Done!" │
                                                 └──────────┘
```

### Bootstrap Stack Resources

| Resource | Type | Purpose |
|----------|------|---------|
| CodeBuild Project | `AWS::CodeBuild::Project` | Runs CDK deployment |
| SNS Topic | `AWS::SNS::Topic` | Sends deployment notifications |
| SNS Subscription | `AWS::SNS::Subscription` | Email notification to user |
| Trigger Lambda | `AWS::Lambda::Function` | Custom resource that starts CodeBuild |
| CodeBuild Role | `AWS::IAM::Role` | Permissions for CDK deployment |
| Lambda Role | `AWS::IAM::Role` | Permissions to start CodeBuild |

### Deployment Flow

1. **User** opens CloudFormation console and creates a stack from `deployments/template.yaml`
2. **CloudFormation** provisions the bootstrap resources (CodeBuild, SNS, Lambda)
3. **Lambda Custom Resource** triggers CodeBuild project
4. **CodeBuild** executes `deployments/buildspec.yml`:
   - Phase 1 (`install`): Install Node.js 18, Python 3.10, AWS CDK, project dependencies
   - Phase 2 (`pre_build`): Build Lambda container image, run unit tests
   - Phase 3 (`build`): `cdk deploy --all --require-approval never`
   - Phase 4 (`post_build`): Publish MCP endpoint URL to SNS, send completion notification
5. **SNS** emails the user with deployment status and MCP endpoint URL
6. **User** copies the endpoint URL to their agent configuration

### CloudFormation Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `StackPrefix` | String | `ndl-ocr` | Prefix for all resource names |
| `NotificationEmail` | String | *(required)* | Email for deployment notifications |
| `LambdaMemoryMB` | Number | `3008` | Lambda memory allocation in MB |
| `LambdaTimeoutSec` | Number | `60` | Lambda timeout in seconds |

## CDK Stack Design

The CDK application is split into two stacks for separation of concerns:

### Stack 1: `OcrLambdaStack`

Provisions the compute and storage layer.

**Resources:**
- **ECR Repository** — Hosts the Lambda container image
- **Lambda Function** — OCR processing function (container image)
- **S3 Bucket** — Image storage with lifecycle policies
- **IAM Role** — Lambda execution role with S3 read + CloudWatch write permissions
- **CloudWatch Log Group** — Lambda logs with 30-day retention
- **CloudWatch Alarms** — Error rate and latency monitoring

**Outputs:**
- Lambda function ARN
- S3 bucket name

### Stack 2: `GatewayStack`

Provisions the MCP interface and authentication layer. Depends on `OcrLambdaStack`.

**Resources:**
- **Amazon Cognito User Pool** — OAuth provider for agent authentication
- **Cognito Resource Server** — Defines `gateway:read` and `gateway:write` scopes
- **Cognito App Client** — M2M client credentials for agent access
- **AgentCore Gateway** — MCP endpoint with Cognito JWT authorizer
- **Gateway Target (Lambda)** — Registers the OCR Lambda as an MCP tool with tool schema
- **IAM Role** — Gateway permissions to invoke Lambda

**Outputs:**
- MCP endpoint URL
- Cognito User Pool ID
- Cognito App Client ID

## Request/Response Flow

### MCP Tool Call: `ocr_extract_text`

**1. Agent sends MCP request to Gateway:**

```json
{
  "method": "tools/call",
  "params": {
    "name": "ocr_extract_text",
    "arguments": {
      "image": "<base64-encoded image/PDF or s3://bucket/key>",
      "pages": "1-3"
    }
  }
}
```

**2. Gateway translates and invokes Lambda with event:**

```json
{
  "image": "<base64 or s3://...>",
  "pages": "1-3"
}
```

**3. Lambda handler (uses cached models — no reload on warm invocations):**
- Decodes base64 / downloads from S3 to numpy array
- If PDF: renders selected pages to images via `pypdfium2` (~0.16s/page)
- For each image: runs `detector.detect()` → `eval_xml()` → `process_cascade()` using cached models
- Assembles per-page JSON, strips `img_path` (local filesystem path leak)

**4. Lambda returns structured response:**

The response wraps NDL-OCR Lite's native JSON format. The `contents` array and field names (`boundingBox`, `isVertical`, `confidence`) come directly from the library — we pass through without transformation.

```json
{
  "statusCode": 200,
  "body": {
    "pages": [
      {
        "page": 1,
        "text": "(z)気送子送付管\n気送子送付には、上記気送管にて...",
        "imginfo": {
          "img_width": 2048,
          "img_height": 1446
        },
        "contents": [
          {
            "id": 0,
            "text": "(z)気送子送付管",
            "boundingBox": [[380,229],[380,251],[569,229],[569,251]],
            "isVertical": "true",
            "isTextline": "true",
            "confidence": 0.895
          }
        ]
      }
    ]
  }
}
```

**Known quirks in the library output (passed through as-is):**
- `isVertical` is hardcoded to `"true"` for every line (library limitation in `ocr.py` line 226)
- `confidence` is 0 for some region types (e.g., page numbers)
- `boundingBox` is 4 corners `[[x1,y1],[x1,y2],[x2,y1],[x2,y2]]`, not `[x1,y1,x2,y2]`

**5. Gateway wraps response as MCP tool result and returns to agent.**

## Security Design

### Network

- AgentCore Gateway endpoint is HTTPS-only
- Lambda runs in a VPC-less configuration (no inbound network access)
- S3 bucket blocks all public access

### Authentication and Authorization

```
Agent ──► AgentCore Gateway (OAuth 2.0 / Cognito) ──► Lambda (IAM invoke) ──► S3 (IAM role)
```

- **Agent to Gateway:** OAuth 2.0 via Amazon Cognito User Pool
  - Cognito resource server defines scopes (`gateway:read`, `gateway:write`)
  - M2M client credentials flow for agent authentication
  - Gateway validates JWT tokens from Cognito
- **Gateway to Lambda:** IAM-based invocation (gateway role has `lambda:InvokeFunction` permission)
- **Lambda to S3:** IAM-based access (execution role has `s3:GetObject` on designated bucket only)

### Data Handling

- Images are decoded to numpy arrays in memory; temp files in `/tmp/<request_id>/` are cleaned up after each invocation
- `img_path` field is stripped from the response to prevent leaking Lambda filesystem paths
- Images in S3 are auto-deleted after 24 hours via lifecycle policy
- No OCR results are cached or stored by the service
- CloudWatch logs may contain request metadata but never image content

## Cost Estimate

For a workload of ~1,000 single-page OCR requests per month (mostly warm invocations):

| Service | Estimate | Notes |
|---------|----------|-------|
| Lambda | ~$0.30 | 1000 invocations x ~3s avg x 3008 MB (warm: ~2s, cold: ~7s) |
| S3 | ~$0.03 | Minimal storage with 24h lifecycle |
| AgentCore Gateway | See pricing | Managed service pricing applies |
| CloudWatch | ~$0.50 | Logs + metrics |
| ECR | ~$0.10 | Container image storage |
| **Total** | **~$1/month** | **+ AgentCore Gateway fees** |

Zero cost when idle (no requests = no Lambda invocations). Model caching reduces cost by ~5x compared to calling `process()` directly (which would average ~7s/invocation).

## Future Considerations

These are **not** in scope for v1 but inform the architecture decisions:

- **Async batch processing:** Use Step Functions to orchestrate large PDF processing beyond Lambda timeout limits
- **GPU acceleration:** Swap to a GPU-enabled Lambda or Fargate task for higher throughput
- **Multi-language:** Swap or augment OCR models when NDL-OCR Lite adds language support
- **Caching:** Add DynamoDB or ElastiCache to avoid re-processing identical images
