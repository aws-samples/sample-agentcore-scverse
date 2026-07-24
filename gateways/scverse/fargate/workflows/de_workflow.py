"""
Differential expression workflow handler — rank_genes_groups with
wilcoxon, volcano plot, dotplot.
"""

import os
import logging

import scanpy as sc
import anndata as ad
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def run_de_workflow(
    input_path: str,
    params: dict,
    output_dir: str,
    job_id: str,
) -> dict:
    """Run differential expression analysis on an .h5ad file.

    Parameters
    ----------
    input_path : str
        Local path to the input .h5ad file.
    params : dict
        Keys: groupby, method, n_genes.
    output_dir : str
        Directory to write plots and results.
    job_id : str
        Job identifier for logging.

    Returns
    -------
    dict
        Contains metrics, plots list, and output filenames.
    """
    logger.info("[%s] Starting DE workflow", job_id)
    os.makedirs(output_dir, exist_ok=True)

    adata = ad.read_h5ad(input_path)

    groupby = params.get("groupby", "leiden")
    method = params.get("method", "wilcoxon")
    n_genes = int(params.get("n_genes", 25))

    # ---- Rank genes groups ---------------------------------------------
    sc.tl.rank_genes_groups(adata, groupby=groupby, method=method)

    # ---- Collect top DE genes per group --------------------------------
    top_de_genes = {}
    n_significant = 0
    for group in adata.obs[groupby].cat.categories:
        names = list(adata.uns["rank_genes_groups"]["names"][group][:n_genes])
        pvals = list(adata.uns["rank_genes_groups"]["pvals_adj"][group][:n_genes])
        sig_count = sum(1 for p in pvals if p < 0.05)
        n_significant += sig_count
        top_de_genes[str(group)] = names

    # ---- Volcano plot --------------------------------------------------
    result = adata.uns["rank_genes_groups"]
    groups = list(adata.obs[groupby].cat.categories)
    if len(groups) > 0:
        # Plot volcano for the first group as representative
        group = groups[0]
        logfc = np.array(result["logfoldchanges"][group], dtype=float)
        pvals = np.array(result["pvals_adj"][group], dtype=float)
        # Avoid log10(0)
        pvals = np.clip(pvals, 1e-300, 1.0)

        fig, ax = plt.subplots(figsize=(8, 6))
        ax.scatter(logfc, -np.log10(pvals), s=2, alpha=0.5, color="grey")
        sig_mask = pvals < 0.05
        ax.scatter(
            logfc[sig_mask], -np.log10(pvals[sig_mask]),
            s=3, alpha=0.7, color="red", label="FDR < 0.05",
        )
        ax.axhline(y=-np.log10(0.05), color="k", linestyle="--", linewidth=0.5)
        ax.set_xlabel("Log2 Fold Change")
        ax.set_ylabel("-log10(adjusted p-value)")
        ax.set_title(f"Volcano Plot — {groupby} group {group}")
        ax.legend()
        plt.savefig(
            os.path.join(output_dir, "de_volcano.png"),
            dpi=150, bbox_inches="tight",
        )
        plt.close()

    # ---- Dotplot --------------------------------------------------------
    sc.pl.rank_genes_groups_dotplot(adata, n_genes=5, show=False)
    plt.savefig(
        os.path.join(output_dir, "de_dotplot.png"),
        dpi=150, bbox_inches="tight",
    )
    plt.close()

    # ---- Save dataset with DE results ----------------------------------
    output_h5ad = os.path.join(output_dir, "de_results.h5ad")
    adata.write_h5ad(output_h5ad)

    logger.info("[%s] DE complete — %d significant genes", job_id, n_significant)

    return {
        "metrics": {
            "n_groups": len(groups),
            "groupby": groupby,
            "method": method,
            "n_significant_genes": n_significant,
            "top_de_genes_per_group": top_de_genes,
        },
        "plots": ["de_volcano.png", "de_dotplot.png"],
        "output_files": ["de_results.h5ad"],
    }
