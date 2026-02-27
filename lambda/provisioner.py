"""EFS provisioner for CDK Custom Resource.

Copies vendor files (models, source, config) and installs Python
dependencies onto the EFS mount at /mnt/models. Runs only during
`cdk deploy`, not at OCR inference time.

Expected EFS layout after provisioning:
    /mnt/models/
      ├─ src/          NDL-OCR source (ocr.py, deim.py, …)
      ├─ model/        4 ONNX model files
      ├─ config/       NDLmoji.yaml, ndl.yaml
      └─ python/       pip-installed packages
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EFS_ROOT = "/mnt/models"
# Vendor files are bundled alongside this handler
_HANDLER_DIR = os.path.dirname(__file__)
_VENDOR_SRC = os.path.join(_HANDLER_DIR, "vendor", "ndlocr-lite", "src")
_REQUIREMENTS = os.path.join(_HANDLER_DIR, "requirements.txt")


def _send_cfn_response(event: dict, context, status: str, reason: str = "") -> None:
    """Send response to CloudFormation signed URL."""
    # Use a stable PhysicalResourceId so CloudFormation doesn't treat
    # updates as replacements (which would trigger Delete of the "old" resource).
    physical_id = event.get("PhysicalResourceId", "efs-provisioner-singleton")
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch log stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
    }).encode("utf-8")

    req = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        headers={"Content-Type": ""},
        method="PUT",
    )
    urllib.request.urlopen(req)


def _copy_vendor_files() -> None:
    """Copy source, models, and config from bundled vendor to EFS."""
    mappings = {
        "src": ["ocr.py", "deim.py", "parseq.py", "ndl_parser.py"],
        "model": [f for f in os.listdir(os.path.join(_VENDOR_SRC, "model"))
                  if f.endswith(".onnx")],
        "config": ["NDLmoji.yaml", "ndl.yaml"],
    }

    for subdir, files in mappings.items():
        dest = os.path.join(EFS_ROOT, subdir)
        os.makedirs(dest, exist_ok=True)
        src_base = os.path.join(_VENDOR_SRC, subdir) if subdir != "src" else _VENDOR_SRC
        for fname in files:
            src_path = os.path.join(src_base, fname)
            dst_path = os.path.join(dest, fname)
            logger.info("Copying %s -> %s", src_path, dst_path)
            shutil.copy2(src_path, dst_path)

    # Copy reading_order directory
    ro_src = os.path.join(_VENDOR_SRC, "reading_order")
    ro_dst = os.path.join(EFS_ROOT, "src", "reading_order")
    if os.path.isdir(ro_src):
        if os.path.exists(ro_dst):
            shutil.rmtree(ro_dst)
        shutil.copytree(ro_src, ro_dst)
        logger.info("Copied reading_order/ directory")


def _install_python_deps() -> None:
    """pip install requirements to EFS python/ directory."""
    target = os.path.join(EFS_ROOT, "python")
    os.makedirs(target, exist_ok=True)

    cmd = [
        "pip", "install",
        "--target", target,
        "--upgrade",
        "--requirement", _REQUIREMENTS,
        "--no-cache-dir",
        "--quiet",
    ]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        logger.error("pip stderr: %s", result.stderr)
        raise RuntimeError(f"pip install failed: {result.stderr}")

    logger.info("pip install completed successfully")


def handler(event: dict, context) -> None:
    """CloudFormation Custom Resource handler."""
    request_type = event.get("RequestType", "Create")
    logger.info("Request type: %s", request_type)

    try:
        if request_type in ("Create", "Update"):
            _copy_vendor_files()
            _install_python_deps()
            logger.info("EFS provisioning complete")
        elif request_type == "Delete":
            # Clean up EFS contents
            for subdir in ("src", "model", "config", "python"):
                path = os.path.join(EFS_ROOT, subdir)
                if os.path.exists(path):
                    shutil.rmtree(path)
                    logger.info("Removed %s", path)

        _send_cfn_response(event, context, "SUCCESS")

    except Exception as e:
        logger.exception("Provisioning failed")
        _send_cfn_response(event, context, "FAILED", str(e))
