"""
Real scanpy preprocessing for the Compute Lambda.

Performs normalization, HVG selection, PCA, neighbor graph computation,
UMAP embedding, Leiden clustering, and differential expression analysis.
"""

import os

import scanpy as sc
import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def run_scanpy_preprocessing(
    local_h5ad_path: str, params: dict, output_dir: str
) -> dict:
    """
    Run scanpy preprocessing pipeline on an .h5ad file.

    Parameters
    ----------
    local_h5ad_path : str
        Path to the input .h5ad file on disk (typically QC-filtered).
    params : dict
        Preprocessing parameters — keys: n_hvgs, n_pcs, n_neighbors,
        leiden_resolution.
    output_dir : str
        Directory to write plots and the preprocessed .h5ad.

    Returns
    -------
    dict
        Contains ``preprocessing_summary``, ``plots`` (list of filenames),
        and ``preprocessed_h5ad`` filename.
    """
    adata = ad.read_h5ad(local_h5ad_path)

    n_cells_input = adata.n_obs
    n_genes_input = adata.n_vars

    # ---- Normalize -----------------------------------------------------
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)

    # ---- Highly variable genes -----------------------------------------
    n_hvgs = int(params.get("n_hvgs", 2000))
    hvg_flavor = "seurat_v3" if "counts" in adata.layers else "seurat"
    sc.pp.highly_variable_genes(
        adata, n_top_genes=n_hvgs, flavor=hvg_flavor,
    )
    n_hvgs_found = int(adata.var["highly_variable"].sum())

    # ---- Store raw and subset to HVGs ----------------------------------
    adata.raw = adata
    adata = adata[:, adata.var.highly_variable].copy()

    # ---- Scale ---------------------------------------------------------
    sc.pp.scale(adata, max_value=10)

    # ---- PCA -----------------------------------------------------------
    n_pcs = int(params.get("n_pcs", 50))
    sc.tl.pca(adata, svd_solver="arpack", n_comps=min(n_pcs, adata.n_vars - 1))

    # ---- Neighbors -----------------------------------------------------
    n_neighbors = int(params.get("n_neighbors", 15))
    n_pcs_neighbors = int(params.get("n_pcs", 40))
    sc.pp.neighbors(
        adata,
        n_neighbors=n_neighbors,
        n_pcs=min(n_pcs_neighbors, adata.obsm["X_pca"].shape[1]),
    )

    # ---- UMAP ----------------------------------------------------------
    sc.tl.umap(adata)

    # ---- Leiden clustering ---------------------------------------------
    leiden_resolution = float(params.get("leiden_resolution", 1.0))
    sc.tl.leiden(adata, resolution=leiden_resolution)
    n_clusters = len(adata.obs["leiden"].unique())

    # ---- Differential expression ---------------------------------------
    sc.tl.rank_genes_groups(adata, "leiden", method="wilcoxon")

    # Collect top DE genes per cluster
    top_de_genes = {}
    for cluster in adata.obs["leiden"].cat.categories:
        gene_names = adata.uns["rank_genes_groups"]["names"][cluster][:5]
        top_de_genes[str(cluster)] = list(gene_names)

    # ---- UMAP plot -----------------------------------------------------
    sc.pl.umap(adata, color=["leiden"], show=False)
    plt.savefig(
        os.path.join(output_dir, "preprocessing_umap_leiden.png"),
        dpi=150,
        bbox_inches="tight",
    )
    plt.close()

    # ---- DE dotplot ----------------------------------------------------
    sc.pl.rank_genes_groups_dotplot(adata, n_genes=5, show=False)
    plt.savefig(
        os.path.join(output_dir, "preprocessing_de_dotplot.png"),
        dpi=150,
        bbox_inches="tight",
    )
    plt.close()

    # ---- Save preprocessed dataset -------------------------------------
    adata.write_h5ad(os.path.join(output_dir, "preprocessed.h5ad"))

    # ---- Build result dict ---------------------------------------------
    return {
        "preprocessing_summary": {
            "n_cells": adata.n_obs,
            "n_genes_input": n_genes_input,
            "n_hvgs": n_hvgs_found,
            "n_pcs": adata.obsm["X_pca"].shape[1],
            "n_neighbors": n_neighbors,
            "leiden_resolution": leiden_resolution,
            "n_clusters": n_clusters,
            "top_de_genes_per_cluster": top_de_genes,
        },
        "plots": [
            "preprocessing_umap_leiden.png",
            "preprocessing_de_dotplot.png",
        ],
        "preprocessed_h5ad": "preprocessed.h5ad",
    }
