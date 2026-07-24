"""
Fargate workflow handlers for the full scverse stack.

Each handler follows the signature:
    run_X_workflow(input_path: str, params: dict, output_dir: str, job_id: str) -> dict
"""

from workflows.qc_workflow import run_qc_workflow
from workflows.integration_workflow import run_integration_workflow
from workflows.clustering_workflow import run_clustering_workflow
from workflows.de_workflow import run_de_workflow
from workflows.spatial_workflow import run_spatial_workflow
from workflows.annotation_workflow import run_annotation_workflow
from workflows.velocity_workflow import run_velocity_workflow
from workflows.perturbation_workflow import run_perturbation_workflow

__all__ = [
    "run_qc_workflow",
    "run_integration_workflow",
    "run_clustering_workflow",
    "run_de_workflow",
    "run_spatial_workflow",
    "run_annotation_workflow",
    "run_velocity_workflow",
    "run_perturbation_workflow",
]
