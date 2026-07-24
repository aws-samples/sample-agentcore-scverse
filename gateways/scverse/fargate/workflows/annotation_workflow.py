"""
Cell type annotation workflow handler — celltypist model download,
annotate, predicted_labels.
"""

import os
import logging

import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import celltypist
from celltypist import models as ct_models

logger = logging.getLogger(__name__)


def run_annotation_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run celltypist cell type annotation on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file.
    params : dict
        Keys: model_name.
    output_dir : str
        Directory to write results.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, and output filenames.
    """
    logger.info("[%s] Starting annotation workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    # ---- Download celltypist model -------------------------------------
    model_name = params.get("model_name", "Immune_All_Low.pkl")
    ct_models.download_models(model=model_name)
    model = ct_models.Model.load(model=model_name)

    # ---- Annotate ------------------------------------------------------
    predictions = celltypist.annotate(adata, model=model)
    adata.obs["celltypist_label"] = predictions.predicted_labels["predicted_labels"]

    # ---- Count cell types ----------------------------------------------
    cell_type_counts = adata.obs["celltypist_label"].value_counts().to_dict()
    n_cell_types = len(cell_type_counts)

    # ---- UMAP colored by cell type (if UMAP exists) --------------------
    plots = []
    if "X_umap" in adata.obsm:
        import scanpy as sc
        sc.pl.umap(adata, color=["celltypist_label"], show=False)
        plt.savefig(
            os.path.join(output_dir, "annotation_umap.png"),
            dpi=150, bbox_inches="tight",
        )
        plt.close()
        plots.append("annotation_umap.png")

    # ---- Save annotated dataset ----------------------------------------
    output_h5ad = os.path.join(output_dir, "annotated.h5ad")
    adata.write_h5ad(output_h5ad)

    logger.info("[%s] Annotation complete — %d cell types", job_id, n_cell_types)

    return {
        "metrics": {
            "n_cells": adata.n_obs,
            "model_name": model_name,
            "n_cell_types": n_cell_types,
            "cell_type_counts": cell_type_counts,
        },
        "plots": plots,
        "output_files": ["annotated.h5ad"],
    }
