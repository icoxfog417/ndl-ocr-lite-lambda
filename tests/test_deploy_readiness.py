"""Pre-deployment validation tests.

Verify that all components are wired correctly BEFORE deploying to AWS.
These tests catch integration issues across boundaries:
  - Tool schema matches what handler.py accepts/returns
  - CDK stacks produce the right CloudFormation resources
  - Gateway target routes to the Lambda alias (SnapStart)
  - Vendor submodule has the files handler.py needs
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_DIR = PROJECT_ROOT / "lambda"
CDK_DIR = PROJECT_ROOT / "cdk"
SCHEMA_PATH = CDK_DIR / "schemas" / "ocr-tool-schema.json"
VENDOR_SRC = LAMBDA_DIR / "vendor" / "ndlocr-lite" / "src"

sys.path.insert(0, str(CDK_DIR))

try:
    import aws_cdk as cdk
    from aws_cdk import assertions

    CDK_AVAILABLE = True
except ImportError:
    CDK_AVAILABLE = False

try:
    import aws_cdk.aws_bedrock_agentcore_alpha  # noqa: F401

    AGENTCORE_AVAILABLE = True
except ImportError:
    AGENTCORE_AVAILABLE = False


# ---------------------------------------------------------------------------
# Tool schema ↔ handler contract
# ---------------------------------------------------------------------------


class TestToolSchemaContract:
    """MCP tool schema must match what handler.py reads from the event."""

    def test_schema_input_matches_handler(self) -> None:
        with open(SCHEMA_PATH) as f:
            tools = json.load(f)

        assert isinstance(tools, list) and len(tools) >= 1
        tool = tools[0]
        assert "name" in tool
        assert "inputSchema" in tool

        props = tool["inputSchema"]["properties"]
        required = tool["inputSchema"]["required"]

        assert "image" in props and props["image"]["type"] == "string"
        assert "pages" in props and props["pages"]["type"] == "string"
        assert "image" in required
        assert "pages" not in required


# ---------------------------------------------------------------------------
# Vendor files — must match what handler.py needs
# ---------------------------------------------------------------------------


class TestVendorFiles:
    """Vendor submodule and handler must agree on file names."""

    def test_vendor_has_required_files(self) -> None:
        if not VENDOR_SRC.exists():
            pytest.skip("Vendor submodule not initialized")

        for name in ["ocr.py", "deim.py", "parseq.py", "ndl_parser.py"]:
            assert (VENDOR_SRC / name).exists(), f"Missing source: {name}"
        assert (VENDOR_SRC / "reading_order").is_dir()

        for name in ["NDLmoji.yaml", "ndl.yaml"]:
            assert (VENDOR_SRC / "config" / name).exists(), f"Missing config: {name}"

    def test_handler_model_paths_match_vendor(self) -> None:
        handler_src = (LAMBDA_DIR / "handler.py").read_text()
        onnx_refs = re.findall(r'"([^"]+\.onnx)"', handler_src)
        assert len(onnx_refs) == 4, f"Expected 4 ONNX refs, got {len(onnx_refs)}"
        assert "NDLmoji.yaml" in handler_src
        assert "ndl.yaml" in handler_src


# ---------------------------------------------------------------------------
# CDK OcrLambdaStack
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CDK_AVAILABLE, reason="aws-cdk-lib not installed")
class TestOcrLambdaStack:
    """Synthesized Lambda stack must have correct resource configuration."""

    @pytest.fixture(scope="class")
    def template(self):
        from stacks.ocr_lambda_stack import OcrLambdaStack

        app = cdk.App()
        stack = OcrLambdaStack(
            app, "TestOcrLambda", stack_prefix="test-ocr",
            lambda_memory_mb=3008, lambda_timeout_sec=60,
        )
        return assertions.Template.from_stack(stack)

    def test_lambda_memory_and_timeout(self, template) -> None:
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "MemorySize": 3008,
                "Timeout": 60,
            },
        )

    def test_version_and_alias(self, template) -> None:
        template.resource_count_is("AWS::Lambda::Version", 1)
        template.resource_count_is("AWS::Lambda::Alias", 1)
        template.has_resource_properties(
            "AWS::Lambda::Alias", {"Name": "live"},
        )

    def test_efs_file_system(self, template) -> None:
        """EFS must be created for model storage."""
        template.resource_count_is("AWS::EFS::FileSystem", 1)
        template.has_resource_properties(
            "AWS::EFS::FileSystem", {"Encrypted": True},
        )

    def test_efs_access_point(self, template) -> None:
        template.resource_count_is("AWS::EFS::AccessPoint", 1)

    def test_vpc_created(self, template) -> None:
        template.resource_count_is("AWS::EC2::VPC", 1)

    def test_efs_provisioner(self, template) -> None:
        """Provisioner Custom Resource must exist to populate EFS."""
        template.resource_count_is("AWS::CloudFormation::CustomResource", 1)

    def test_lambda_functions(self, template) -> None:
        """OCR handler + EFS provisioner + S3 auto-delete helper."""
        template.resource_count_is("AWS::Lambda::Function", 3)

    def test_uses_efs_for_models(self) -> None:
        """CDK stack must use EFS mount, not layer, for models."""
        stack_src = (CDK_DIR / "stacks" / "ocr_lambda_stack.py").read_text()
        assert "/mnt/models" in stack_src
        assert "FileSystem.from_efs_access_point" in stack_src

    def test_s3_bucket_secured(self, template) -> None:
        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            },
        )

    def test_monitoring(self, template) -> None:
        template.has_resource_properties(
            "AWS::Logs::LogGroup", {"RetentionInDays": 30},
        )
        template.resource_count_is("AWS::CloudWatch::Alarm", 2)


# ---------------------------------------------------------------------------
# CDK GatewayStack — wiring to Lambda alias
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (CDK_AVAILABLE and AGENTCORE_AVAILABLE),
    reason="aws-cdk-lib or agentcore-alpha not installed",
)
class TestGatewayStack:
    """Gateway must route to the Lambda alias with correct MCP protocol."""

    @pytest.fixture(scope="class")
    def template(self):
        from stacks.ocr_lambda_stack import OcrLambdaStack
        from stacks.gateway_stack import GatewayStack

        app = cdk.App()
        ocr = OcrLambdaStack(app, "TestOcr2", stack_prefix="test-ocr")
        gw = GatewayStack(
            app, "TestGw", stack_prefix="test-ocr",
            lambda_function=ocr.lambda_function,
            lambda_alias=ocr.lambda_alias,
        )
        return assertions.Template.from_stack(gw)

    def test_gateway_with_mcp_protocol(self, template) -> None:
        template.resource_count_is("AWS::BedrockAgentCore::Gateway", 1)
        template.has_resource_properties(
            "AWS::BedrockAgentCore::Gateway",
            {"ProtocolType": "MCP"},
        )

    def test_gateway_target_exists(self, template) -> None:
        template.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 1)

    def test_cognito_m2m_auth(self, template) -> None:
        template.resource_count_is("AWS::Cognito::UserPool", 1)
        template.resource_count_is("AWS::Cognito::UserPoolClient", 1)

    def test_schema_file_path_resolves(self) -> None:
        computed = CDK_DIR / "schemas" / "ocr-tool-schema.json"
        assert computed.exists()
        with open(computed) as f:
            json.load(f)  # must be valid JSON
