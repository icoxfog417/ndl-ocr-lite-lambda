#!/usr/bin/env python3
"""CDK app entry point for NDL-OCR Lite Lambda service."""

import os

import aws_cdk as cdk

from stacks.ocr_lambda_stack import OcrLambdaStack
from stacks.gateway_stack import GatewayStack

app = cdk.App()

stack_prefix = app.node.try_get_context("stack_prefix") or os.environ.get(
    "STACK_PREFIX", "ndl-ocr"
)
lambda_memory = int(
    app.node.try_get_context("lambda_memory") or os.environ.get("LAMBDA_MEMORY_MB", "3008")
)
lambda_timeout = int(
    app.node.try_get_context("lambda_timeout") or os.environ.get("LAMBDA_TIMEOUT_SEC", "60")
)

ocr_stack = OcrLambdaStack(
    app,
    f"{stack_prefix}-lambda",
    stack_prefix=stack_prefix,
    lambda_memory_mb=lambda_memory,
    lambda_timeout_sec=lambda_timeout,
)

gateway_stack = GatewayStack(
    app,
    f"{stack_prefix}-gateway",
    stack_prefix=stack_prefix,
    lambda_function=ocr_stack.lambda_function,
    lambda_alias=ocr_stack.lambda_alias,
)
gateway_stack.add_dependency(ocr_stack)

app.synth()
