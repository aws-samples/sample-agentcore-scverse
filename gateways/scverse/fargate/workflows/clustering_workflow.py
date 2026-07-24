"""
Clustering workflow handler — neighbors, Leiden, UMAP, rank_genes_groups.
"""

import os
import logging

import scanpy as sc
import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def run_clustering_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run clustering analysis on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file.
    params : dict
        Keys: n_neighbors, n_pcs, leiden_resolution.
    output_dir : str
        Directory to write plots and the clustered .h5ad.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, and output filenames.
    """
    logger.info("[%s] Starting clustering workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    # ---- Neighbors -----------------------------------------------------
    n_neighbors = int(params.get("n_neighbors", 15))
    n_pcs = int(params.get("n_pcs", 40))
    use_rep = "X_scVI" if "X_scVI" in adata.obsm else None
    sc.pp.neighbors(
        adata,
        n_neighbors=n_neighbors,
        n_pcs=n_pcs if use_rep is None else None,
        use_rep=use_rep,
    )

    # ---- Leiden clustering ---------------------------------------------
    leiden_resolution = float(params.get("leiden_resolution", 1.0))
    sc.tl.leiden(adata, resolution=leiden_resolution)
    n_clusters = len(adata.obs["leiden"].unique())

    # ---- UMAP ----------------------------------------------------------
    sc.tl.umap(adata)

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
        os.path.join(output_dir, "clustering_umap_leiden.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- DE dotplot ----------------------------------------------------
    sc.pl.rank_genes_groups_dotplot(adata, n_genes=5, show=False)
    plt.savefig(
        os.path.join(output_dir, "clustering_de_dotplot.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- Save clustered dataset ----------------------------------------
    output_h5ad = os.path.join(output_dir, "clustered.h5ad")
    adata.write_h5ad(output_h5ad)

    logger.info("[%s] Clustering complete — %d clusters", job_id, n_clusters)

    return {
        "metrics": {
            "n_cells": adata.n_obs,
            "n_clusters": n_clusters,
            "leiden_resolution": leiden_resolution,
            "top_de_genes_per_cluster": top_de_genes,
        },
        "plots": [
            "clustering_umap_leiden.png",
            "clustering_de_dotplot.png",
        ],
        "output_files": ["clustered.h5ad"],
    }
