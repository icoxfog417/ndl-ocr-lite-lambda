# NDL-OCR Lite MCP Lambda

One-click deployable OCR service that brings [NDL-OCR Lite](https://github.com/ndl-lab/ndlocr-lite) to your AI agent via AWS Lambda and [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html).

**Allow your desktop agent to read anything you have.**

## What is this?

This project packages Japan's National Diet Library OCR engine (NDL-OCR Lite) as an AWS Lambda function and exposes it through AgentCore Gateway as an MCP (Model Context Protocol) tool. Any MCP-compatible AI agent — Claude Desktop, Cline, your custom agent — can call this tool to extract text from images of books, documents, and scanned pages.

### Key Features

- **Accurate Japanese OCR** — Powered by NDL-OCR Lite's three-stage pipeline: layout recognition (DEIMv2), character recognition (PARSeq), and reading order sequencing
- **MCP-native** — Exposed as an MCP tool through AgentCore Gateway; agents discover and call it like any other tool
- **One-click deploy** — Deploy the entire stack from the AWS CloudFormation console with no local tooling required
- **Serverless** — Runs on AWS Lambda with no servers to manage; scales to zero when idle
- **Multi-format support** — Accepts JPG, PNG, TIFF, JP2, and BMP images

## Architecture Overview

```
┌─────────────┐     MCP      ┌───────────────────┐     invoke     ┌─────────────────┐
│  AI Agent   │◄────────────►│  AgentCore        │──────────────►│  OCR Lambda     │
│  (Desktop)  │   protocol   │  Gateway          │               │  (NDL-OCR Lite) │
└─────────────┘              └───────────────────┘               └────────┬────────┘
                                                                          │
                                                                          ▼
                                                                 ┌─────────────────┐
                                                                 │  S3 Bucket      │
                                                                 │  (image store)  │
                                                                 └─────────────────┘
```

The full architecture is documented in [spec/design.md](spec/design.md).

## Quick Start (One-Click Deploy)

### Prerequisites

- An AWS account
- Sufficient IAM permissions to create CloudFormation stacks, Lambda functions, S3 buckets, and AgentCore Gateway resources

### Deploy

1. Click the **Launch Stack** button below (or upload `deployments/template.yaml` to CloudFormation)
2. Fill in the parameters (stack name, notification email)
3. Acknowledge IAM capability creation and launch the stack
4. Wait for the completion email (~10–15 minutes)
5. Copy the MCP endpoint URL from the stack outputs

<!-- TODO: Add Launch Stack button once deployment region is finalized -->
<!-- [![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/new?stackName=ndl-ocr-mcp&templateURL=<S3_TEMPLATE_URL>) -->

### Connect Your Agent

Add the MCP endpoint to your agent's configuration. For Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ndl-ocr": {
      "url": "<MCP_ENDPOINT_URL from stack outputs>"
    }
  }
}
```

Then ask your agent: *"Read the text from this scanned page"* and attach an image.

## Developer Setup

### Prerequisites

- Python 3.10+
- Node.js 18+ and npm (for CDK)
- AWS CLI configured with credentials
- AWS CDK CLI (`npm install -g aws-cdk`)

### Project Structure

```
ndl-ocr-lite-lambda/
├── README.md
├── spec/
│   ├── requirements.md          # User stories and requirements
│   └── design.md                # AWS architecture design
├── lambda/
│   ├── handler.py               # Lambda entry point
│   ├── ocr_engine.py            # NDL-OCR Lite integration
│   └── requirements.txt         # Python dependencies
├── cdk/
│   ├── app.py                   # CDK app entry point
│   └── stacks/
│       ├── ocr_lambda_stack.py  # Lambda + S3 stack
│       └── gateway_stack.py     # AgentCore Gateway stack
├── deployments/
│   ├── template.yaml            # CloudFormation one-click template
│   └── buildspec.yml            # CodeBuild spec for CDK deploy
└── tests/
    ├── unit/                    # Unit tests
    └── integration/             # Integration tests
```

### Local Development

```bash
# Clone
git clone https://github.com/icoxfog417/ndl-ocr-lite-lambda.git
cd ndl-ocr-lite-lambda

# Set up Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r lambda/requirements.txt

# Install CDK dependencies
cd cdk && pip install -r requirements.txt && cd ..

# Deploy to your AWS account
cd cdk && cdk deploy --all
```

### Running Tests

```bash
pytest tests/unit
pytest tests/integration  # requires AWS credentials
```

## MCP Tool Interface

The Lambda exposes the following MCP tool through AgentCore Gateway:

### `ocr_extract_text`

Extract text from an image using NDL-OCR Lite.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `image` | string | Yes | Base64-encoded image data or S3 URI (`s3://bucket/key`) |
| `format` | string | No | Image format hint (`jpg`, `png`, `tiff`, `jp2`, `bmp`). Auto-detected if omitted |
| `include_layout` | boolean | No | If `true`, return layout regions with bounding boxes. Default: `false` |

**Response:**

```json
{
  "text": "Extracted full text in reading order...",
  "regions": [
    {
      "text": "Region text",
      "bbox": [x1, y1, x2, y2],
      "type": "body"
    }
  ]
}
```

## Requirements and Design

- [User Stories and Requirements](spec/requirements.md)
- [AWS Architecture Design](spec/design.md)

## References

- [NDL-OCR Lite](https://github.com/ndl-lab/ndlocr-lite) — National Diet Library OCR engine
- [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html) — Managed MCP gateway service
- [One-Click Generative AI Solutions](https://github.com/aws-samples/sample-one-click-generative-ai-solutions) — One-click deployment pattern reference

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

NDL-OCR Lite models and code are subject to the [NDL-OCR Lite license](https://github.com/ndl-lab/ndlocr-lite/blob/main/LICENSE).
