"""
Perturbation analysis workflow handler — pertpy perturbation analysis.
"""

import os
import logging

import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pertpy as pt

logger = logging.getLogger(__name__)


def run_perturbation_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run pertpy perturbation analysis on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file.
    params : dict
        Keys: perturbation_key, control_key, method.
    output_dir : str
        Directory to write plots and results.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, and output filenames.
    """
    logger.info("[%s] Starting perturbation workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    perturbation_key = params.get("perturbation_key", "perturbation")
    control_key = params.get("control_key", "control")
    method = params.get("method", "edistance")

    # ---- Perturbation distance analysis --------------------------------
    if method == "edistance":
        distance = pt.tools.Distance(metric="edistance")
        df_distances = distance.pairwise(
            adata,
            groupby=perturbation_key,
        )

        # ---- Heatmap of perturbation distances -------------------------
        fig, ax = plt.subplots(figsize=(10, 8))
        import numpy as np
        im = ax.imshow(df_distances.values, cmap="viridis", aspect="auto")
        ax.set_xticks(range(len(df_distances.columns)))
        ax.set_xticklabels(df_distances.columns, rotation=90, fontsize=6)
        ax.set_yticks(range(len(df_distances.index)))
        ax.set_yticklabels(df_distances.index, fontsize=6)
        ax.set_title("Perturbation Distance Matrix")
        plt.colorbar(im, ax=ax)
        plt.savefig(
            os.path.join(output_dir, "perturbation_distances.png"),
            dpi=150, bbox_inches="tight",
        )
        plt.close()

        n_perturbations = len(df_distances.columns)
    else:
        n_perturbations = len(adata.obs[perturbation_key].unique())

    # ---- Save dataset --------------------------------------------------
    output_h5ad = os.path.join(output_dir, "perturbation_results.h5ad")
    adata.write_h5ad(output_h5ad)

    logger.info("[%s] Perturbation analysis complete", job_id)

    plots = []
    if method == "edistance":
        plots.append("perturbation_distances.png")

    return {
        "metrics": {
            "n_cells": adata.n_obs,
            "perturbation_key": perturbation_key,
            "method": method,
            "n_perturbations": n_perturbations,
        },
        "plots": plots,
        "output_files": ["perturbation_results.h5ad"],
    }
