"""OcrLambdaStack: Lambda function, layer, S3 bucket, CloudWatch alarms."""

from __future__ import annotations

import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

# Resolve paths relative to the project root (parent of cdk/).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


class OcrLambdaStack(Stack):
    """Provisions the OCR Lambda function, model layer, S3 bucket, and monitoring."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stack_prefix: str,
        lambda_memory_mb: int = 3008,
        lambda_timeout_sec: int = 60,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- S3 Bucket for large image uploads ---
        self.bucket = s3.Bucket(
            self,
            "ImageBucket",
            bucket_name=None,  # auto-generated
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(1)),
            ],
            versioned=False,
        )

        # --- Lambda Layer (models + dependencies) ---
        self.layer = lambda_.LayerVersion(
            self,
            "OcrModelLayer",
            code=lambda_.Code.from_asset(os.path.join(_PROJECT_ROOT, "layers", "ocr-models")),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="NDL-OCR Lite models, source, and Python dependencies",
        )

        # --- CloudWatch Log Group ---
        log_group = logs.LogGroup(
            self,
            "LambdaLogGroup",
            log_group_name=f"/aws/lambda/{stack_prefix}-ocr",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Lambda Function ---
        self.lambda_function = lambda_.Function(
            self,
            "OcrFunction",
            function_name=f"{stack_prefix}-ocr",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                os.path.join(_PROJECT_ROOT, "lambda"),
                exclude=["vendor", "*.pyc", "__pycache__"],
            ),
            layers=[self.layer],
            memory_size=lambda_memory_mb,
            timeout=Duration.seconds(lambda_timeout_sec),
            architecture=lambda_.Architecture.X86_64,
            environment={
                "LAMBDA_LAYER_DIR": "/opt",
                "NDLOCR_SRC_DIR": "/opt/src",
            },
            log_group=log_group,
            snap_start=lambda_.SnapStartConf.ON_PUBLISHED_VERSIONS,
        )

        # Grant S3 read access
        self.bucket.grant_read(self.lambda_function)

        # Publish a version and create alias for SnapStart
        version = self.lambda_function.current_version
        self.lambda_alias = lambda_.Alias(
            self,
            "LiveAlias",
            alias_name="live",
            version=version,
        )

        # --- CloudWatch Alarms ---
        error_alarm = cloudwatch.Alarm(
            self,
            "ErrorRateAlarm",
            metric=self.lambda_function.metric_errors(
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description=f"Error rate > 5% over 5 minutes for {stack_prefix}-ocr",
        )

        duration_alarm = cloudwatch.Alarm(
            self,
            "DurationAlarm",
            metric=self.lambda_function.metric_duration(
                period=Duration.minutes(5),
                statistic="p95",
            ),
            threshold=30_000,  # 30 seconds in ms
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description=f"p95 duration > 30s for {stack_prefix}-ocr",
        )

        # --- Outputs ---
        cdk.CfnOutput(
            self,
            "LambdaFunctionArn",
            value=self.lambda_alias.function_arn,
            description="Lambda alias ARN (SnapStart-enabled)",
        )
        cdk.CfnOutput(
            self,
            "S3BucketName",
            value=self.bucket.bucket_name,
            description="S3 bucket for large image uploads",
        )
