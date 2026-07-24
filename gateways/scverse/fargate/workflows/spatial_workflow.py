"""
Spatial analysis workflow handler — squidpy spatial_neighbors,
nhood_enrichment, spatial_scatter.
"""

import os
import logging

import scanpy as sc
import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import squidpy as sq

logger = logging.getLogger(__name__)


def run_spatial_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run squidpy spatial analysis on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file (must contain spatial coordinates).
    params : dict
        Keys: cluster_key, coord_type, n_neighs.
    output_dir : str
        Directory to write plots and results.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, and output filenames.
    """
    logger.info("[%s] Starting spatial workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    cluster_key = params.get("cluster_key", "leiden")
    coord_type = params.get("coord_type", "generic")
    n_neighs = int(params.get("n_neighs", 6))

    # ---- Spatial neighbors ---------------------------------------------
    sq.gr.spatial_neighbors(
        adata,
        coord_type=coord_type,
        n_neighs=n_neighs,
    )

    # ---- Neighborhood enrichment ---------------------------------------
    sq.gr.nhood_enrichment(adata, cluster_key=cluster_key)

    # ---- Spatial scatter plot ------------------------------------------
    sq.pl.spatial_scatter(adata, color=cluster_key, show=False)
    plt.savefig(
        os.path.join(output_dir, "spatial_scatter.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- Neighborhood enrichment heatmap -------------------------------
    sq.pl.nhood_enrichment(adata, cluster_key=cluster_key, show=False)
    plt.savefig(
        os.path.join(output_dir, "spatial_nhood_enrichment.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- Save dataset with spatial results -----------------------------
    output_h5ad = os.path.join(output_dir, "spatial_results.h5ad")
    adata.write_h5ad(output_h5ad)

    logger.info("[%s] Spatial analysis complete", job_id)

    return {
        "metrics": {
            "n_cells": adata.n_obs,
            "cluster_key": cluster_key,
            "n_spatial_neighbors": n_neighs,
        },
        "plots": [
            "spatial_scatter.png",
            "spatial_nhood_enrichment.png",
        ],
        "output_files": ["spatial_results.h5ad"],
    }
