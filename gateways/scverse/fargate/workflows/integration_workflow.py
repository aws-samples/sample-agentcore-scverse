"""
Integration workflow handler — scvi-tools SCVI model training,
latent representation, UMAP, and Leiden clustering.
"""

import os
import json
import logging

import scanpy as sc
import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import scvi

logger = logging.getLogger(__name__)


def run_integration_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run scvi-tools integration on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file.
    params : dict
        Keys: batch_key, n_latent, n_hvgs, max_epochs, leiden_resolution.
    output_dir : str
        Directory to write plots and the integrated .h5ad.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, and output filenames.
    """
    logger.info("[%s] Starting integration workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    # ---- HVG selection -------------------------------------------------
    n_hvgs = int(params.get("n_hvgs", 2000))
    sc.pp.highly_variable_genes(
        adata,
        n_top_genes=n_hvgs,
        flavor="seurat_v3" if "counts" in adata.layers else "seurat",
        subset=True,
    )

    # ---- Setup and train SCVI model ------------------------------------
    batch_key = params.get("batch_key")
    scvi.model.SCVI.setup_anndata(adata, batch_key=batch_key)

    n_latent = int(params.get("n_latent", 10))
    model = scvi.model.SCVI(adata, n_latent=n_latent)

    max_epochs = int(params.get("max_epochs", 50))
    model.train(max_epochs=max_epochs)

    # ---- Latent representation -----------------------------------------
    adata.obsm["X_scVI"] = model.get_latent_representation()

    # ---- Neighbors, UMAP, Leiden on scVI embedding ---------------------
    sc.pp.neighbors(adata, use_rep="X_scVI")
    sc.tl.umap(adata)

    leiden_resolution = float(params.get("leiden_resolution", 1.0))
    sc.tl.leiden(adata, resolution=leiden_resolution)
    n_clusters = len(adata.obs["leiden"].unique())

    # ---- UMAP plot -----------------------------------------------------
    sc.pl.umap(adata, color=["leiden"], show=False)
    plt.savefig(
        os.path.join(output_dir, "integration_umap_leiden.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # If batch_key is present, also plot by batch
    if batch_key and batch_key in adata.obs.columns:
        sc.pl.umap(adata, color=[batch_key], show=False)
        plt.savefig(
            os.path.join(output_dir, "integration_umap_batch.png"),
            dpi=150, bbox_inches="tight",
        )
        plt.close()

    # ---- Save integrated dataset ---------------------------------------
    output_h5ad = os.path.join(output_dir, "integrated.h5ad")
    adata.write_h5ad(output_h5ad)

    # ---- Save metrics --------------------------------------------------
    metrics = {
        "n_cells": adata.n_obs,
        "n_hvgs": n_hvgs,
        "n_latent": n_latent,
        "max_epochs": max_epochs,
        "n_clusters": n_clusters,
        "leiden_resolution": leiden_resolution,
    }
    with open(os.path.join(output_dir, "integration_metrics.json"), "w") as fh:
        json.dump(metrics, fh, indent=2)

    logger.info("[%s] Integration complete — %d clusters", job_id, n_clusters)

    plots = ["integration_umap_leiden.png"]
    if batch_key and batch_key in adata.obs.columns:
        plots.append("integration_umap_batch.png")

    return {
        "metrics": metrics,
        "plots": plots,
        "output_files": ["integrated.h5ad", "integration_metrics.json"],
    }
