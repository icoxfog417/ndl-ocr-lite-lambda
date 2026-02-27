# NDL-OCR Lite MCP Lambda

One-click deployable OCR service that brings [NDL-OCR Lite](https://github.com/ndl-lab/ndlocr-lite) to your AI agent via AWS Lambda and [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html).

**Allow your desktop agent to read anything you have.**

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create?stackName=ndl-ocr-mcp&templateURL=https://raw.githubusercontent.com/icoxfog417/ndl-ocr-lite-lambda/main/deployments/template.yaml)

## What is this?

This project wraps [NDL-OCR Lite](https://github.com/ndl-lab/ndlocr-lite) — Japan's National Diet Library OCR engine — in a thin AWS Lambda handler and exposes it as an MCP (Model Context Protocol) tool through AgentCore Gateway. Any MCP-compatible AI agent (Claude Code, Claude Desktop, your custom agent) can call this tool to extract text from images and PDFs of books, documents, and scanned pages.

NDL-OCR Lite already provides the complete OCR pipeline: layout recognition, character recognition, reading order sequencing, and structured output. This project's job is to **make that pipeline callable from any AI agent with one-click deployment**.

### Key Features

- **Accurate Japanese OCR** — Powered by NDL-OCR Lite (DEIMv2 layout detection, PARSeq character recognition cascade, XY-Cut reading order)
- **Image and PDF support** — Accepts JPG, PNG, TIFF, JP2, BMP images and multi-page PDFs
- **MCP-native** — Exposed as an MCP tool through AgentCore Gateway; agents discover and call it like any other tool
- **One-click deploy** — Deploy the entire stack from the AWS CloudFormation console with no local tooling required
- **Serverless** — Runs on AWS Lambda with no servers to manage; scales to zero when idle
- **EFS-backed** — ONNX models (~147MB) and Python dependencies live on EFS, eliminating Lambda size limits

## Architecture Overview

```
                              MCP (SigV4)           invoke
┌──────────────┐          ┌──────────────┐       ┌──────────────┐       ┌─────────┐
│   AI Agent   │◄────────►│  AgentCore   │──────►│  OCR Lambda  │──────►│   EFS   │
│ (Claude Code)│ mcp-proxy│  Gateway     │       │  (handler)   │       │ /models │
└──────────────┘  for-aws └──────────────┘       └──────┬───────┘       │ /python │
                                                        │               │ /src    │
                                                        ▼               └─────────┘
                                                 ┌──────────────┐
                                                 │  S3 Bucket   │
                                                 │ (large files)│
                                                 └──────────────┘
```

**How it works:**

1. Lambda handler (~7KB) loads 4 ONNX models from EFS at `/mnt/models`
2. Warm invocations reuse module-level model objects (no reload)
3. A CDK Custom Resource automatically populates EFS during deployment (copies vendor files + pip installs dependencies)
4. Receive image/PDF (base64 or S3 URI) → detect layout → recognize characters → return structured JSON

The full architecture is documented in [spec/design.md](spec/design.md).

## Quick Start (One-Click Deploy)

### Prerequisites

- An AWS account
- Sufficient IAM permissions to create CloudFormation stacks, Lambda functions, VPC, EFS, S3 buckets, and AgentCore Gateway resources

### Deploy

1. Click the **Launch Stack** button above (deploys to us-east-1)
2. Fill in the parameters (stack prefix, notification email)
3. Acknowledge IAM capability creation and launch the stack
4. Wait for the completion email (~10-15 minutes)
5. Copy the MCP endpoint URL from the stack outputs

### Connect Your Agent

Install [mcp-proxy-for-aws](https://github.com/aws/mcp-proxy-for-aws) and add the MCP endpoint to your agent's configuration.

**For Claude Code (`.mcp.json`):**

```json
{
  "mcpServers": {
    "ndl-ocr": {
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws@latest",
        "<MCP_ENDPOINT_URL from stack outputs>",
        "--service", "bedrock-agentcore",
        "--region", "us-east-1"
      ]
    }
  }
}
```

Then ask your agent: *"Read the text from this scanned page"* and attach an image.

## Developer Setup

### Prerequisites

- Python 3.12+
- Node.js 18+ and npm (for CDK)
- [uv](https://docs.astral.sh/uv/) package manager
- AWS CLI configured with credentials
- AWS CDK CLI (`npm install -g aws-cdk`)

### Project Structure

```
ndl-ocr-lite-lambda/
├── lambda/
│   ├── handler.py               # Lambda entry point (thin wrapper)
│   ├── ocr_engine.py            # ONNX model loading and inference
│   ├── input_parser.py          # Base64/S3/PDF input handling
│   ├── pdf_utils.py             # PDF page rendering
│   ├── provisioner.py           # EFS provisioner (CDK Custom Resource)
│   └── vendor/ndlocr-lite/      # NDL-OCR Lite submodule
├── cdk/
│   ├── app.py                   # CDK app entry point
│   ├── schemas/                 # MCP tool schema
│   └── stacks/
│       ├── ocr_lambda_stack.py  # Lambda + VPC + EFS + S3 + monitoring
│       └── gateway_stack.py     # AgentCore Gateway (IAM auth)
├── layers/
│   └── requirements.txt         # Python dependencies for EFS
├── deployments/
│   └── template.yaml            # CloudFormation one-click template
├── tests/
│   ├── test_deploy_readiness.py # Pre-deployment validation (17 tests)
│   └── test_handler_e2e.py      # End-to-end handler tests
└── spec/
    ├── requirements.md          # User stories and requirements
    └── design.md                # AWS architecture design
```

### Local Development

```bash
# Clone with submodules
git clone --recursive https://github.com/icoxfog417/ndl-ocr-lite-lambda.git
cd ndl-ocr-lite-lambda

# Install dependencies
uv sync --group dev --group cdk

# Run tests
uv run pytest tests/ -v

# Deploy to your AWS account
cd cdk && uv run cdk deploy --all
```

## MCP Tool Interface

The Lambda exposes the following MCP tool through AgentCore Gateway:

### `ocr_extract_text`

Extract text from an image or PDF using NDL-OCR Lite.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `image` | string | Yes | Base64-encoded image/PDF data or S3 URI (`s3://bucket/key`). Supports JPG, PNG, TIFF, JP2, BMP, and PDF. |
| `pages` | string | No | Page range for PDFs (e.g. `1-3`, `1,3,5`). Default: all pages |

**Response:**

```json
{
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
```

## Requirements and Design

- [User Stories and Requirements](spec/requirements.md)
- [AWS Architecture Design](spec/design.md)

## References

- [NDL-OCR Lite](https://github.com/ndl-lab/ndlocr-lite) — National Diet Library OCR engine (CC BY 4.0)
- [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html) — Managed MCP gateway service
- [mcp-proxy-for-aws](https://github.com/aws/mcp-proxy-for-aws) — SigV4 authentication proxy for MCP clients

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

NDL-OCR Lite models and code are licensed under [CC BY 4.0](https://github.com/ndl-lab/ndlocr-lite/blob/master/LICENSE).
