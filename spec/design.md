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

### 2. AWS Lambda Function (Thin Wrapper)

**Role:** Receives input from Gateway, passes images to NDL-OCR Lite's `process()`, returns results.

**Runtime Configuration:**
- Runtime: Python 3.10
- Memory: 3008 MB (required for ML model inference)
- Timeout: 60 seconds
- Architecture: x86_64
- Packaging: Container image (to accommodate model weights exceeding 250 MB zip limit)

**Processing Flow:**

The Lambda handler is intentionally thin. NDL-OCR Lite's `process()` handles all OCR logic.

```
Request received (image or PDF, base64 or S3 URI)
       │
       ├── base64? ──► decode to /tmp/input/
       │
       ├── S3 URI? ──► download from S3 to /tmp/input/
       │
       ├── PDF? ──────► split into page images via pypdfium2
       │                write each page to /tmp/input/
       │
       ▼
┌──────────────────────────────────────────────┐
│  NDL-OCR Lite process()                      │
│                                              │
│  args.sourcedir = /tmp/input/                │
│  args.output    = /tmp/output/               │
│                                              │
│  (library handles layout detection,          │
│   character recognition cascade,             │
│   reading order, JSON/XML/TXT output)        │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
              Read /tmp/output/*.json
              Assemble into response
              Return { pages: [...] }
```

**NDL-OCR Lite Pipeline Detail:**

The OCR engine runs a three-stage pipeline for each image:

1. **Layout Recognition (DEIMv2)** — `deim-s-1024x1024.onnx` detects 18 region classes including body text, headings, captions, tables, running headers, and page numbers. Outputs bounding boxes with confidence scores.
2. **Reading Order (XY-Cut)** — Recursive bisection algorithm assigns logical reading order to detected regions.
3. **Text Recognition (PARSeq cascade)** — Three ONNX models of increasing capacity:
   - `parseq_30.onnx` (256px, ~30 chars) — tries first, escalates at 25+ chars
   - `parseq_50.onnx` (384px, ~50 chars) — escalates at 45+ chars
   - `parseq_100.onnx` (768px, ~100 chars) — terminal model

   Lines are routed to the smallest sufficient model first, with automatic escalation. Recognition within each tier uses thread-pool parallelism.

**Lambda Container Image:**

The container image bundles:
- Python 3.10 runtime
- NDL-OCR Lite source code and dependencies
- ONNX Runtime 1.23+ (CPU)
- Pillow, NumPy, lxml, networkx, PyYAML
- Pre-trained ONNX model weights:
  - `deim-s-1024x1024.onnx` (layout recognition)
  - `parseq_30.onnx`, `parseq_50.onnx`, `parseq_100.onnx` (character recognition cascade)
  - `NDLmoji.yaml` (character vocabulary)

**Why container image over zip:**
NDL-OCR Lite's model weights (~500 MB+) exceed Lambda's 250 MB deployment package limit. Container images support up to 10 GB, providing ample room for models and dependencies.

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

**3. Lambda handler:**
- Decodes/downloads the input to `/tmp/input/`
- If PDF: splits into page images via `pypdfium2`, applies `pages` filter
- Calls `process(args)` with `args.sourcedir=/tmp/input/`, `args.output=/tmp/output/`
- Reads the generated `*.json` files from `/tmp/output/`

**4. Lambda returns NDL-OCR Lite's native JSON output:**

```json
{
  "statusCode": 200,
  "body": {
    "pages": [
      {
        "page": 1,
        "text": "Full text in reading order...",
        "imginfo": {
          "img_width": 2000,
          "img_height": 3000
        },
        "contents": [
          {
            "id": 0,
            "text": "Line text",
            "boundingBox": [[x1,y1],[x1,y2],[x2,y1],[x2,y2]],
            "isVertical": "true",
            "isTextline": "true",
            "confidence": 0.95
          }
        ]
      }
    ]
  }
}
```

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

- Images sent via base64 are processed in-memory and never persisted
- Images in S3 are auto-deleted after 24 hours via lifecycle policy
- No OCR results are cached or stored by the service
- CloudWatch logs may contain request metadata but never image content

## Cost Estimate

For a workload of ~1,000 OCR requests per month:

| Service | Estimate | Notes |
|---------|----------|-------|
| Lambda | ~$1.50 | 1000 invocations x 15s avg x 3008 MB |
| S3 | ~$0.03 | Minimal storage with 24h lifecycle |
| AgentCore Gateway | See pricing | Managed service pricing applies |
| CloudWatch | ~$0.50 | Logs + metrics |
| ECR | ~$0.10 | Container image storage |
| **Total** | **~$2–3/month** | **+ AgentCore Gateway fees** |

Zero cost when idle (no requests = no Lambda invocations).

## Future Considerations

These are **not** in scope for v1 but inform the architecture decisions:

- **Async batch processing:** Use Step Functions to orchestrate large PDF processing beyond Lambda timeout limits
- **GPU acceleration:** Swap to a GPU-enabled Lambda or Fargate task for higher throughput
- **Multi-language:** Swap or augment OCR models when NDL-OCR Lite adds language support
- **Caching:** Add DynamoDB or ElastiCache to avoid re-processing identical images
