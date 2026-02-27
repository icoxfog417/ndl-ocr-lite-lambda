"""OcrLambdaStack: Lambda + EFS (models + deps) + S3 + monitoring.

Architecture:
  - Everything heavy lives on EFS (mounted at /mnt/models):
    models, NDL-OCR source, config, and Python dependencies.
  - Lambda code is just the thin handler (~7KB).
  - A provisioner Custom Resource populates EFS during deployment:
    copies vendor files and pip-installs dependencies.
  - VPC is required for EFS access; NAT gateway enables S3/CloudWatch reach.
  - Warm invocations reuse module-level model objects (no reload).
"""

from __future__ import annotations

import hashlib
import os

import aws_cdk as cdk
from aws_cdk import (
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_ec2 as ec2,
    aws_efs as efs,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


class OcrLambdaStack(Stack):
    """Provisions OCR Lambda with EFS for all heavy assets."""

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

        # --- VPC (required for EFS) ---
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
        )

        # --- EFS for models, source, config, and Python deps ---
        file_system = efs.FileSystem(
            self,
            "ModelFs",
            vpc=vpc,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.ELASTIC,
            removal_policy=RemovalPolicy.DESTROY,
            encrypted=True,
        )

        access_point = file_system.add_access_point(
            "LambdaAccess",
            path="/lambda",
            create_acl=efs.Acl(owner_uid="1001", owner_gid="1001", permissions="755"),
            posix_user=efs.PosixUser(uid="1001", gid="1001"),
        )

        # --- S3 Bucket for large image uploads ---
        self.bucket = s3.Bucket(
            self,
            "ImageBucket",
            bucket_name=None,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(expiration=Duration.days(1)),
            ],
            versioned=False,
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
        efs_mount = lambda_.FileSystem.from_efs_access_point(
            access_point, "/mnt/models",
        )

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
            memory_size=lambda_memory_mb,
            timeout=Duration.seconds(lambda_timeout_sec),
            architecture=lambda_.Architecture.X86_64,
            environment={
                "LAMBDA_LAYER_DIR": "/mnt/models",
                "NDLOCR_SRC_DIR": "/mnt/models/src",
                "IMAGE_BUCKET": self.bucket.bucket_name,
                # Add EFS python packages to PYTHONPATH
                "PYTHONPATH": "/mnt/models/python",
            },
            vpc=vpc,
            filesystem=efs_mount,
            log_group=log_group,
            # Note: SnapStart is incompatible with EFS in current CDK.
            # Warm invocations reuse module-level model objects regardless.
        )

        # Grant S3 read/write access (write for presigned upload URLs)
        self.bucket.grant_read_write(self.lambda_function)

        # --- EFS Provisioner (Custom Resource) ---
        # Populates EFS with vendor files and pip-installed dependencies.
        # Bundles the full vendor submodule (~148MB) in its deployment package.
        provisioner_fn = lambda_.Function(
            self,
            "EfsProvisioner",
            function_name=f"{stack_prefix}-efs-provisioner",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="provisioner.handler",
            code=lambda_.Code.from_asset(
                os.path.join(_PROJECT_ROOT, "lambda"),
                exclude=["*.pyc", "__pycache__"],
            ),
            memory_size=1024,
            ephemeral_storage_size=cdk.Size.gibibytes(2),
            timeout=Duration.minutes(15),
            architecture=lambda_.Architecture.X86_64,
            vpc=vpc,
            filesystem=efs_mount,
        )

        # Hash requirements.txt and provisioner code to trigger re-provisioning
        hasher = hashlib.sha256()
        for fname in ("layers/requirements.txt", "lambda/provisioner.py"):
            fpath = os.path.join(_PROJECT_ROOT, fname)
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    hasher.update(f.read())
        provision_hash = hasher.hexdigest()[:16]

        CustomResource(
            self,
            "EfsProvisionerCR",
            service_token=provisioner_fn.function_arn,
            properties={
                "ProvisionHash": provision_hash,
            },
        )

        # Publish a version and create alias
        version = self.lambda_function.current_version
        self.lambda_alias = lambda_.Alias(
            self,
            "LiveAlias",
            alias_name="live",
            version=version,
        )

        # --- CloudWatch Alarms ---
        cloudwatch.Alarm(
            self,
            "ErrorRateAlarm",
            metric=self.lambda_function.metric_errors(
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description=f"Error rate > 5 over 5 minutes for {stack_prefix}-ocr",
        )

        cloudwatch.Alarm(
            self,
            "DurationAlarm",
            metric=self.lambda_function.metric_duration(
                period=Duration.minutes(5),
                statistic="p95",
            ),
            threshold=30_000,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description=f"p95 duration > 30s for {stack_prefix}-ocr",
        )

        # --- Outputs ---
        self.file_system = file_system
        self.vpc = vpc

        cdk.CfnOutput(
            self,
            "LambdaFunctionArn",
            value=self.lambda_alias.function_arn,
            description="Lambda alias ARN",
        )
        cdk.CfnOutput(
            self,
            "S3BucketName",
            value=self.bucket.bucket_name,
            description="S3 bucket for large image uploads",
        )
        cdk.CfnOutput(
            self,
            "EfsFileSystemId",
            value=file_system.file_system_id,
            description="EFS file system ID — upload models and deps here",
        )
