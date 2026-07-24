"""
QC workflow handler for ECS Fargate.

Replicates the same scanpy QC logic as the Compute Lambda scanpy_qc.py:
MAD-based filtering, violin plots, threshold plots.
"""

import os
import logging

import scanpy as sc
import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import median_abs_deviation

logger = logging.getLogger(__name__)


def run_qc_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run scanpy QC on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file.
    params : dict
        QC parameters — species, mad_counts, mad_genes, mad_mt,
        mt_threshold, min_cells.
    output_dir : str
        Directory to write plots and the filtered .h5ad.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, output filenames, and step info.
    """
    logger.info("[%s] Starting QC workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    # ---- Detect mitochondrial genes ------------------------------------
    mt_key = "MT-" if params.get("species", "human") == "human" else "mt-"
    adata.var["mt"] = adata.var_names.str.startswith(mt_key)

    # ---- Calculate QC metrics ------------------------------------------
    sc.pp.calculate_qc_metrics(
        adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True,
    )

    # ---- Before-filtering stats ----------------------------------------
    n_cells_before = adata.n_obs
    n_genes_before = adata.n_vars
    mean_counts = float(adata.obs["total_counts"].mean())
    median_counts = float(adata.obs["total_counts"].median())
    mean_mt_pct = float(adata.obs["pct_counts_mt"].mean())

    # ---- Before-filtering violin plot ----------------------------------
    sc.pl.violin(
        adata,
        ["n_genes_by_counts", "total_counts", "pct_counts_mt"],
        jitter=0.4,
        multi_panel=True,
        show=False,
    )
    plt.savefig(
        os.path.join(output_dir, "qc_violin_before_filtering.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- MAD-based filtering parameters --------------------------------
    mad_counts = float(params.get("mad_counts", 5))
    mad_genes = float(params.get("mad_genes", 5))
    mad_mt = float(params.get("mad_mt", 3))
    mt_threshold = float(params.get("mt_threshold", 8))
    min_cells = int(params.get("min_cells", 20))

    # ---- Compute MAD thresholds ----------------------------------------
    counts_median = np.median(adata.obs["total_counts"])
    counts_mad = median_abs_deviation(adata.obs["total_counts"])
    counts_upper = counts_median + mad_counts * counts_mad

    genes_median = np.median(adata.obs["n_genes_by_counts"])
    genes_mad = median_abs_deviation(adata.obs["n_genes_by_counts"])
    genes_upper = genes_median + mad_genes * genes_mad

    mt_median = np.median(adata.obs["pct_counts_mt"])
    mt_mad = median_abs_deviation(adata.obs["pct_counts_mt"])
    mt_upper_mad = mt_median + mad_mt * mt_mad
    mt_upper = min(mt_upper_mad, mt_threshold)

    # ---- Apply basic filters -------------------------------------------
    sc.pp.filter_cells(adata, min_genes=200)
    sc.pp.filter_genes(adata, min_cells=min_cells)

    # ---- Apply MAD-based outlier removal -------------------------------
    keep_counts = adata.obs["total_counts"] <= counts_upper
    keep_genes = adata.obs["n_genes_by_counts"] <= genes_upper
    keep_mt = adata.obs["pct_counts_mt"] <= mt_upper
    adata = adata[keep_counts & keep_genes & keep_mt, :].copy()

    # ---- Threshold plot ------------------------------------------------
    fig, ax = plt.subplots(1, 1, figsize=(8, 6))
    ax.scatter(
        adata.obs["total_counts"],
        adata.obs["pct_counts_mt"],
        s=1, alpha=0.5,
    )
    ax.axhline(y=mt_upper, color="r", linestyle="--",
               label=f"MT threshold ({mt_upper:.1f}%)")
    ax.axvline(x=counts_upper, color="b", linestyle="--",
               label=f"Counts upper ({counts_upper:.0f})")
    ax.set_xlabel("Total counts")
    ax.set_ylabel("% mitochondrial counts")
    ax.set_title("QC Thresholds")
    ax.legend()
    plt.savefig(
        os.path.join(output_dir, "qc_thresholds.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- After-filtering violin plot -----------------------------------
    sc.pl.violin(
        adata,
        ["n_genes_by_counts", "total_counts", "pct_counts_mt"],
        jitter=0.4,
        multi_panel=True,
        show=False,
    )
    plt.savefig(
        os.path.join(output_dir, "qc_violin_after_filtering.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- Save filtered dataset -----------------------------------------
    output_h5ad = os.path.join(output_dir, "filtered.h5ad")
    adata.write_h5ad(output_h5ad)

    logger.info("[%s] QC complete — %d → %d cells", job_id, n_cells_before, adata.n_obs)

    return {
        "metrics": {
            "n_cells_before": n_cells_before,
            "n_cells_after": adata.n_obs,
            "n_genes_before": n_genes_before,
            "n_genes_after": adata.n_vars,
            "cells_removed": n_cells_before - adata.n_obs,
            "cells_retained_pct": round(adata.n_obs / n_cells_before * 100, 1),
            "mean_counts_per_cell": round(mean_counts, 1),
            "median_counts_per_cell": round(median_counts, 1),
            "mean_mt_pct": round(mean_mt_pct, 2),
        },
        "plots": [
            "qc_violin_before_filtering.png",
            "qc_thresholds.png",
            "qc_violin_after_filtering.png",
        ],
        "output_files": ["filtered.h5ad"],
    }
