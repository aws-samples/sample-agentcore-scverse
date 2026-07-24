"""
RNA velocity workflow handler — scvelo filter_and_normalize, moments,
velocity, velocity_graph, velocity_embedding_stream.
"""

import os
import logging

import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import scvelo as scv

logger = logging.getLogger(__name__)


def run_velocity_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run scvelo RNA velocity analysis on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file (must contain spliced/unspliced layers).
    params : dict
        Keys: mode, n_top_genes.
    output_dir : str
        Directory to write plots and results.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, and output filenames.
    """
    logger.info("[%s] Starting velocity workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    mode = params.get("mode", "stochastic")
    n_top_genes = int(params.get("n_top_genes", 2000))

    # ---- Preprocessing -------------------------------------------------
    scv.pp.filter_and_normalize(adata, n_top_genes=n_top_genes)
    scv.pp.moments(adata)

    # ---- Velocity estimation -------------------------------------------
    scv.tl.velocity(adata, mode=mode)
    scv.tl.velocity_graph(adata)

    # ---- Velocity embedding stream plot --------------------------------
    scv.pl.velocity_embedding_stream(adata, basis="umap", show=False)
    plt.savefig(
        os.path.join(output_dir, "velocity_embedding_stream.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- Save dataset with velocity results ----------------------------
    output_h5ad = os.path.join(output_dir, "velocity_results.h5ad")
    adata.write_h5ad(output_h5ad)

    logger.info("[%s] Velocity analysis complete", job_id)

    return {
        "metrics": {
            "n_cells": adata.n_obs,
            "mode": mode,
            "n_top_genes": n_top_genes,
        },
        "plots": ["velocity_embedding_stream.png"],
        "output_files": ["velocity_results.h5ad"],
    }
