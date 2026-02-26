# CLAUDE.md

## Mission

**Allow your desktop agent to read anything you have.**

This project wraps [NDL-OCR Lite](https://github.com/ndl-lab/ndlocr-lite) in a thin AWS Lambda handler and exposes it as an MCP tool via [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html). One-click deployable. See [spec/requirements.md](spec/requirements.md) for user stories and [spec/design.md](spec/design.md) for architecture.

## Development Environment

### Prerequisites

- **Python**: 3.10+ (3.11 available in this environment)
- **uv**: Package manager (0.8+). All Python execution MUST go through uv.
- **Node.js**: 18+ (for AWS CDK)
- **Git**: For version control

### Sandbox

Use the `.sandbox/` directory for experimentation, prototyping, and testing NDL-OCR Lite locally. This directory is gitignored. Clone dependencies, run test scripts, and iterate here before promoting code to `lambda/` or `cdk/`.

```bash
# Example: clone NDL-OCR Lite into sandbox for local testing
cd .sandbox
git clone https://github.com/ndl-lab/ndlocr-lite.git
cd ndlocr-lite
uv run --with -r requirements.txt python src/ocr.py --sourceimg test.jpg --output /tmp/out
```

## Development Policies

### Python Execution

**Always use `uv run python` to run Python.** Never use bare `python` or `python3`.

```bash
# Correct
uv run python handler.py
uv run pytest tests/

# Wrong
python handler.py
python3 -m pytest tests/
```

For scripts that need specific dependencies:

```bash
uv run --with boto3 --with pillow python my_script.py
```

### Package Management

- Use `uv` for all dependency management (no pip, no conda)
- Each component (`lambda/`, `cdk/`) has its own `pyproject.toml` or `requirements.txt`
- Lock files should be committed for reproducibility

### Code Conventions

- This project is a **thin wrapper**. Do NOT reimplement NDL-OCR Lite logic.
- Lambda handler receives input, passes it to `process()`, returns the JSON output. Keep it minimal.
- Infrastructure is AWS CDK (Python). CDK stacks live in `cdk/stacks/`.
- Tests use pytest. Run with `uv run pytest`.

### Directory Responsibilities

```
.sandbox/          → Experimentation (gitignored). Clone NDL-OCR Lite here, test locally.
lambda/            → Lambda handler code. Thin wrapper around NDL-OCR Lite.
cdk/               → CDK infrastructure code.
deployments/       → One-click CloudFormation templates and CodeBuild specs.
spec/              → Requirements and architecture docs. Keep in sync with code.
tests/             → Unit and integration tests.
```

### Git

- Develop on the assigned feature branch
- Commit messages: imperative mood, explain "why" not "what"
- Do not commit `.sandbox/` contents, `.env`, or credentials
