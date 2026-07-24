"""
ECS Fargate worker entry point for the full scverse stack.

Reads a job manifest from S3, executes the requested workflow,
uploads outputs, and updates the DynamoDB Job Table throughout.
"""

import os
import sys
import json
import time
import threading
import logging
import tempfile
import base64
import glob
from datetime import datetime

import boto3
import jinja2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------
s3_client = boto3.client("s3")

# ---------------------------------------------------------------------------
# Workflow imports (lazy — imported at module level for container startup)
# ---------------------------------------------------------------------------
from workflows import (
    run_qc_workflow,
    run_integration_workflow,
    run_clustering_workflow,
    run_de_workflow,
    run_spatial_workflow,
    run_annotation_workflow,
    run_velocity_workflow,
    run_perturbation_workflow,
)

WORKFLOW_HANDLERS = {
    "qc": run_qc_workflow,
    "preprocessing": run_qc_workflow,  # QC includes preprocessing in Fargate
    "integration": run_integration_workflow,
    "clustering": run_clustering_workflow,
    "differential_expression": run_de_workflow,
    "spatial": run_spatial_workflow,
    "annotation": run_annotation_workflow,
    "velocity": run_velocity_workflow,
    "perturbation": run_perturbation_workflow,
}


# ---------------------------------------------------------------------------
# DynamoDB helpers — self-contained, no import from gateway function
# ---------------------------------------------------------------------------
_dynamodb = boto3.resource("dynamodb")
_job_table = None


def _get_job_table():
    """Lazy-init the DynamoDB Table resource."""
    global _job_table
    if _job_table is None:
        table_name = os.environ.get("JOB_TABLE_NAME", "scverse-job-table")
        _job_table = _dynamodb.Table(table_name)
    return _job_table


def update_job_status(
    job_id: str,
    status: str = None,
    current_step_name: str = None,
    output_s3_keys: list = None,
    error_message: str = None,
    error_step: str = None,
    start_timestamp: str = None,
    completion_timestamp: str = None,
    heartbeat_timestamp: str = None,
) -> None:
    """Update a job record in the Job Table.

    Builds the UpdateExpression dynamically so only non-None
    parameters are written.
    """
    table = _get_job_table()

    expr_parts = []
    values = {}
    names = {}

    if status is not None:
        expr_parts.append("#st = :status")
        values[":status"] = status
        names["#st"] = "status"

    _optional = {
        "current_step_name": current_step_name,
        "output_s3_keys": output_s3_keys,
        "error_message": error_message,
        "error_step": error_step,
        "start_timestamp": start_timestamp,
        "completion_timestamp": completion_timestamp,
        "heartbeat_timestamp": heartbeat_timestamp,
    }

    for field_name, field_value in _optional.items():
        if field_value is not None:
            placeholder = f":{field_name}"
            alias = f"#{field_name}"
            expr_parts.append(f"{alias} = {placeholder}")
            values[placeholder] = field_value
            names[alias] = field_name

    if not expr_parts:
        return

    update_expression = "SET " + ", ".join(expr_parts)
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expression,
        ExpressionAttributeValues=values,
        ExpressionAttributeNames=names,
    )


def append_output_keys(job_id: str, new_keys: list) -> None:
    """Append S3 keys to a job's output_s3_keys list."""
    table = _get_job_table()
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #keys = list_append(#keys, :keys)",
        ExpressionAttributeValues={":keys": new_keys},
        ExpressionAttributeNames={"#keys": "output_s3_keys"},
    )


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def heartbeat_loop(job_id: str, interval: int = 30) -> None:
    """Background thread that writes a heartbeat timestamp every *interval* seconds."""
    while True:
        try:
            update_job_status(
                job_id,
                heartbeat_timestamp=datetime.utcnow().isoformat() + "Z",
            )
        except Exception as exc:
            logger.error("Heartbeat update failed for %s: %s", job_id, type(exc).__name__)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Workflow dispatcher
# ---------------------------------------------------------------------------
def run_workflow(
    job_id: str,
    workflow_type: str,
    local_input_path: str,
    params: dict,
) -> dict:
    """Route to the appropriate workflow handler and return its result dict."""
    handler = WORKFLOW_HANDLERS.get(workflow_type)
    if handler is None:
        raise ValueError(
            f"Unsupported workflow_type '{workflow_type}'. "
            f"Valid: {sorted(WORKFLOW_HANDLERS.keys())}"
        )

    output_dir = os.path.join(os.path.dirname(local_input_path), "outputs")
    os.makedirs(output_dir, exist_ok=True)

    return handler(
        input_path=local_input_path,
        params=params,
        output_dir=output_dir,
        job_id=job_id,
    )


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------
_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>scverse Analysis Report — {{ job_id }}</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { border-bottom: 2px solid #0066cc; padding-bottom: .5rem; }
  h2 { color: #0066cc; margin-top: 2rem; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  th, td { border: 1px solid #ddd; padding: .5rem .75rem; text-align: left; }
  th { background: #f5f5f5; }
  .plot { max-width: 100%; margin: 1rem 0; }
  .step { background: #f9f9f9; border-left: 4px solid #0066cc; padding: .75rem 1rem; margin: .5rem 0; }
  .methods { background: #fffbe6; border: 1px solid #ffe066; padding: 1rem; margin-top: 2rem; }
  .timing { color: #666; font-size: .9rem; }
</style>
</head>
<body>
<h1>scverse Analysis Report</h1>
<p><strong>Job ID:</strong> {{ job_id }}</p>
<p><strong>Generated:</strong> {{ generated_at }}</p>

<h2>Pipeline Steps</h2>
{% for step in steps %}
<div class="step">
  <strong>{{ step.name }}</strong>
  <span class="timing">({{ step.duration_seconds | round(1) }}s)</span>
  {% if step.summary %}
  <ul>
  {% for k, v in step.summary.items() %}
    <li><strong>{{ k }}:</strong> {{ v }}</li>
  {% endfor %}
  </ul>
  {% endif %}
</div>
{% endfor %}

<h2>Metrics</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
{% for k, v in metrics.items() %}
<tr><td>{{ k }}</td><td>{{ v }}</td></tr>
{% endfor %}
</table>

<h2>Plots</h2>
{% for plot in plots %}
<h3>{{ plot.title }}</h3>
<img class="plot" src="data:image/png;base64,{{ plot.data_b64 }}" alt="{{ plot.title }}">
{% endfor %}

<div class="methods">
<h2>Methods</h2>
<p>This analysis was performed using the scverse ecosystem on AWS ECS Fargate.</p>
<table>
<tr><th>Package</th><th>Version</th></tr>
{% for pkg, ver in versions.items() %}
<tr><td>{{ pkg }}</td><td>{{ ver }}</td></tr>
{% endfor %}
</table>
</div>
</body>
</html>"""


def _get_scverse_versions() -> dict:
    """Return installed scverse package versions."""
    versions = {}
    for pkg_name in ("scanpy", "anndata", "scvi-tools", "squidpy",
                     "celltypist", "pertpy", "scvelo", "matplotlib",
                     "scipy", "numpy", "pandas"):
        try:
            mod = __import__(pkg_name.replace("-", "_").split(".")[0])
            versions[pkg_name] = getattr(mod, "__version__", "unknown")
        except ImportError:
            versions[pkg_name] = "not installed"
    return versions


def generate_html_report(
    job_id: str,
    steps: list,
    metrics: dict,
    output_dir: str,
) -> str:
    """Render an HTML report with embedded base64 plots and save it.

    Parameters
    ----------
    job_id : str
    steps : list[dict]
        Each dict has ``name``, ``duration_seconds``, and optional ``summary`` dict.
    metrics : dict
        Key-value pairs of aggregate metrics.
    output_dir : str
        Directory containing PNG plots and where the report will be saved.

    Returns
    -------
    str
        Path to the generated HTML file.
    """
    # Collect plots as base64
    plot_entries = []
    for png_path in sorted(glob.glob(os.path.join(output_dir, "*.png"))):
        with open(png_path, "rb") as fh:
            data_b64 = base64.b64encode(fh.read()).decode("ascii")
        title = os.path.splitext(os.path.basename(png_path))[0].replace("_", " ").title()
        plot_entries.append({"title": title, "data_b64": data_b64})

    template = jinja2.Template(_REPORT_TEMPLATE)
    html = template.render(
        job_id=job_id,
        generated_at=datetime.utcnow().isoformat() + "Z",
        steps=steps,
        metrics=metrics,
        plots=plot_entries,
        versions=_get_scverse_versions(),
    )

    report_path = os.path.join(output_dir, "pipeline_report.html")
    with open(report_path, "w") as fh:
        fh.write(html)

    return report_path


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Fargate task entry point.

    1. Download job manifest from S3
    2. Start heartbeat thread
    3. Update status to RUNNING
    4. Download input .h5ad
    5. Execute workflow
    6. Upload outputs
    7. Write output manifest
    8. Append output keys THEN update status to COMPLETED
    """
    # ---- Read environment variables ------------------------------------
    manifest_s3_key = os.environ["JOB_MANIFEST_S3_KEY"]
    storage_bucket = os.environ["STORAGE_BUCKET"]
    # JOB_TABLE_NAME is read by _get_job_table()

    logger.info("Starting Fargate worker — manifest: s3://%s/%s",
                storage_bucket, manifest_s3_key)

    # ---- Download and parse manifest -----------------------------------
    manifest_obj = s3_client.get_object(
        Bucket=storage_bucket, Key=manifest_s3_key,
    )
    manifest = json.loads(manifest_obj["Body"].read().decode("utf-8"))

    job_id = manifest["job_id"]
    workflow_type = manifest["workflow_type"]
    input_s3_key = manifest["input_s3_key"]
    parameters = manifest.get("parameters", {})
    output_prefix = manifest.get(
        "output_prefix", f"scverse-jobs/{job_id}/outputs/"
    )
    notification_email = manifest.get("notification_email")

    logger.info("Job %s — workflow=%s input=%s", job_id, workflow_type, input_s3_key)

    # ---- Start heartbeat thread ----------------------------------------
    hb_thread = threading.Thread(
        target=heartbeat_loop, args=(job_id,), daemon=True,
    )
    hb_thread.start()

    try:
        # ---- Update status to RUNNING ----------------------------------
        update_job_status(
            job_id,
            status="RUNNING",
            start_timestamp=datetime.utcnow().isoformat() + "Z",
        )

        # ---- Download input .h5ad --------------------------------------
        work_dir = tempfile.mkdtemp(prefix=f"{job_id}_")
        local_input = os.path.join(work_dir, "input.h5ad")
        logger.info("Downloading s3://%s/%s → %s", storage_bucket, input_s3_key, local_input)
        s3_client.download_file(storage_bucket, input_s3_key, local_input)

        # ---- Execute workflow ------------------------------------------
        start_time = time.time()
        result = run_workflow(job_id, workflow_type, local_input, parameters)
        elapsed = time.time() - start_time
        logger.info("Workflow completed in %.1fs", elapsed)

        # ---- Generate HTML report --------------------------------------
        output_dir = os.path.join(work_dir, "outputs")
        os.makedirs(output_dir, exist_ok=True)

        steps = result.get("steps", [{"name": workflow_type, "duration_seconds": elapsed}])
        metrics = result.get("metrics", {})
        generate_html_report(job_id, steps, metrics, output_dir)

        # ---- Upload all outputs ----------------------------------------
        output_s3_keys = []
        for local_file in glob.glob(os.path.join(output_dir, "**", "*"), recursive=True):
            if not os.path.isfile(local_file):
                continue
            relative = os.path.relpath(local_file, output_dir)
            s3_key = f"{output_prefix}{relative}"
            s3_client.upload_file(local_file, storage_bucket, s3_key)
            output_s3_keys.append(s3_key)
            logger.info("Uploaded %s → s3://%s/%s", relative, storage_bucket, s3_key)

        # ---- Write output manifest -------------------------------------
        output_manifest = {
            "job_id": job_id,
            "artifacts": [
                {
                    "s3_key": key,
                    "type": _artifact_type(key),
                    "description": os.path.basename(key),
                }
                for key in output_s3_keys
            ],
            "scverse_versions": _get_scverse_versions(),
        }
        manifest_key = f"{output_prefix}manifest.json"
        s3_client.put_object(
            Bucket=storage_bucket,
            Key=manifest_key,
            Body=json.dumps(output_manifest, indent=2),
            ContentType="application/json",
        )
        output_s3_keys.append(manifest_key)

        # ---- Append output keys THEN update status (Req 7.3) ----------
        append_output_keys(job_id, output_s3_keys)
        update_job_status(
            job_id,
            status="COMPLETED",
            completion_timestamp=datetime.utcnow().isoformat() + "Z",
        )
        logger.info("Job %s COMPLETED — %d artifacts uploaded", job_id, len(output_s3_keys))

    except Exception as exc:
        # Log only the error type; the traceback/message can contain
        # user-submitted parameters or file paths.
        logger.error("Job %s FAILED: %s", job_id, type(exc).__name__)
        try:
            update_job_status(
                job_id,
                status="FAILED",
                error_message=str(exc),
                completion_timestamp=datetime.utcnow().isoformat() + "Z",
            )
        except Exception:
            logger.exception("Failed to update job status to FAILED")
        sys.exit(1)


def _artifact_type(s3_key: str) -> str:
    """Infer artifact type from file extension."""
    if s3_key.endswith(".png"):
        return "plot"
    elif s3_key.endswith(".h5ad"):
        return "data"
    elif s3_key.endswith(".html"):
        return "report"
    elif s3_key.endswith(".json"):
        return "metrics"
    return "other"


if __name__ == "__main__":
    main()
