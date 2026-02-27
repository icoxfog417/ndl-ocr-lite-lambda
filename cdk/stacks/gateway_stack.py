"""GatewayStack: AgentCore Gateway with IAM auth and Lambda MCP target."""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_lambda as lambda_,
)
from constructs import Construct

import aws_cdk.aws_bedrock_agentcore_alpha as agentcore


class GatewayStack(Stack):
    """Provisions AgentCore Gateway as an MCP endpoint for the OCR Lambda.

    The Gateway L2 construct auto-creates:
    - IAM-based authorization (SigV4 signing)
    - IAM execution role for the Gateway service
    - Lambda invoke permissions for the target

    Outputs include the MCP endpoint URL. Callers authenticate using
    AWS credentials (SigV4) via mcp-proxy-for-aws.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stack_prefix: str,
        lambda_function: lambda_.Function,
        lambda_alias: lambda_.Alias,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- AgentCore Gateway ---
        # IAM auth enables SigV4 signing via mcp-proxy-for-aws.
        self.gateway = agentcore.Gateway(
            self,
            "OcrGateway",
            gateway_name=f"{stack_prefix}-gateway",
            description="MCP gateway for NDL-OCR Lite OCR service",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
            protocol_configuration=agentcore.McpProtocolConfiguration(
                instructions=(
                    "This gateway provides OCR (optical character recognition) "
                    "for Japanese documents. Use the ocr_extract_text tool to "
                    "extract text from images or PDFs."
                ),
                supported_versions=[agentcore.MCPProtocolVersion.MCP_2025_03_26],
            ),
        )

        # --- Lambda Target with tool schema ---
        # add_lambda_target auto-grants lambda:InvokeFunction to the gateway role.
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "schemas",
            "ocr-tool-schema.json",
        )
        self.gateway.add_lambda_target(
            "OcrTarget",
            gateway_target_name=f"{stack_prefix}-ocr-target",
            description="NDL-OCR Lite Lambda function for Japanese document OCR",
            lambda_function=lambda_alias,
            tool_schema=agentcore.ToolSchema.from_local_asset(schema_path),
        )

        # --- Outputs ---
        cdk.CfnOutput(
            self,
            "GatewayId",
            value=self.gateway.gateway_id,
            description="AgentCore Gateway ID",
        )
        cdk.CfnOutput(
            self,
            "McpEndpointUrl",
            value=f"https://{self.gateway.gateway_id}.gateway.bedrock-agentcore.{self.region}.amazonaws.com/mcp",
            description="MCP endpoint URL for agent configuration",
        )
