"""GatewayStack: AgentCore Gateway with Cognito auth and Lambda MCP target."""

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
    - Cognito User Pool with M2M client credentials flow
    - IAM execution role for the Gateway service
    - Lambda invoke permissions for the target

    Outputs include the MCP endpoint URL and Cognito credentials needed
    for MCP client configuration.
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
        # Default auth auto-creates Cognito User Pool with M2M client credentials.
        # Default protocol is MCP.
        self.gateway = agentcore.Gateway(
            self,
            "OcrGateway",
            gateway_name=f"{stack_prefix}-gateway",
            description="MCP gateway for NDL-OCR Lite OCR service",
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
        cdk.CfnOutput(
            self,
            "CognitoUserPoolId",
            value=self.gateway.user_pool.user_pool_id,
            description="Cognito User Pool ID for authentication",
        )
        cdk.CfnOutput(
            self,
            "CognitoAppClientId",
            value=self.gateway.user_pool_client.user_pool_client_id,
            description="Cognito App Client ID for M2M authentication",
        )
        cdk.CfnOutput(
            self,
            "TokenEndpointUrl",
            value=self.gateway.token_endpoint_url,
            description="Cognito OAuth token endpoint for obtaining access tokens",
        )
        cdk.CfnOutput(
            self,
            "OAuthScopes",
            value=" ".join(self.gateway.oauth_scopes),
            description="OAuth scopes to request when obtaining access tokens",
        )
