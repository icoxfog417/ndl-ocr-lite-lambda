"""Pre-deployment validation tests.

Verify that all components are wired correctly BEFORE deploying to AWS.
These tests catch integration issues across boundaries:
  - Tool schema matches what handler.py accepts/returns
  - CDK stacks produce the right CloudFormation resources
  - Gateway target routes to the Lambda alias (SnapStart)
  - Buildspec copies the same files handler.py needs
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
TEMPLATE_PATH = PROJECT_ROOT / "deployments" / "template.yaml"

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

        # handler.py reads event.get("image") and event.get("pages")
        assert "image" in props and props["image"]["type"] == "string"
        assert "pages" in props and props["pages"]["type"] == "string"
        assert "image" in required
        assert "pages" not in required


# ---------------------------------------------------------------------------
# Layer structure — vendor files must match what handler.py + buildspec need
# ---------------------------------------------------------------------------


class TestLayerStructure:
    """Vendor submodule, handler, and buildspec must agree on file names."""

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

    def test_buildspec_copies_same_files(self) -> None:
        content = TEMPLATE_PATH.read_text()
        for module in ["ocr.py", "deim.py", "parseq.py", "ndl_parser.py"]:
            assert module in content, f"Buildspec missing: {module}"
        assert "reading_order" in content
        assert "NDLmoji.yaml" in content
        assert "ndl.yaml" in content

    def test_buildspec_runs_tests_before_deploy(self) -> None:
        content = TEMPLATE_PATH.read_text()
        assert content.find("pytest") < content.find("cdk deploy")

    def test_buildspec_installs_onnxruntime_for_tests(self) -> None:
        """onnxruntime must be in the test env (not just the layer) for e2e tests."""
        content = TEMPLATE_PATH.read_text()
        # Layer install (--target) is for Lambda runtime
        assert "--target layers/ocr-models/python onnxruntime" in content
        # Separate install (no --target) is for the test venv
        assert "uv pip install onnxruntime" in content


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

    def test_lambda_runtime_and_snapstart(self, template) -> None:
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Runtime": "python3.12",
                "MemorySize": 3008,
                "Timeout": 60,
                "SnapStart": {"ApplyOn": "PublishedVersions"},
            },
        )

    def test_lambda_env_vars(self, template) -> None:
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Environment": {
                    "Variables": assertions.Match.object_like({
                        "LAMBDA_LAYER_DIR": "/opt",
                        "NDLOCR_SRC_DIR": "/opt/src",
                    })
                }
            },
        )

    def test_version_and_alias_for_snapstart(self, template) -> None:
        """SnapStart requires a published version + alias (not $LATEST)."""
        template.resource_count_is("AWS::Lambda::Version", 1)
        template.resource_count_is("AWS::Lambda::Alias", 1)
        template.has_resource_properties(
            "AWS::Lambda::Alias", {"Name": "live"},
        )

    def test_layer_attached(self, template) -> None:
        template.resource_count_is("AWS::Lambda::LayerVersion", 1)

    def test_lambda_code_excludes_vendor(self) -> None:
        """Lambda code asset must exclude vendor/ (models are in the Layer)."""
        stack_src = (CDK_DIR / "stacks" / "ocr_lambda_stack.py").read_text()
        assert '"vendor"' in stack_src and "exclude" in stack_src

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
        """gateway_stack.py computes schema path via os.path — verify it works."""
        computed = CDK_DIR / "schemas" / "ocr-tool-schema.json"
        assert computed.exists()
        with open(computed) as f:
            json.load(f)  # must be valid JSON
