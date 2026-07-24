"""
Property-based tests for Compute Lambda QC response structure.

Validates:
- Property 2: QC response structure completeness

Requirements: 1.6
"""

import importlib
import json
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

# Ensure the compute directory is on the import path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Mock heavy scientific dependencies before importing scanpy_qc
# ---------------------------------------------------------------------------

def _install_mock_modules():
    """
    Inject mock modules for scanpy, anndata, matplotlib, and scipy into
    sys.modules so that ``import scanpy_qc`` succeeds without the real
    packages installed.
    """
    mock_scanpy = MagicMock()
    mock_anndata = MagicMock()
    mock_matplotlib = MagicMock()
    mock_matplotlib_pyplot = MagicMock()
    mock_scipy = MagicMock()
    mock_scipy_stats = MagicMock()

    # matplotlib.use("Agg") must be a no-op
    mock_matplotlib.use = MagicMock()

    mods = {
        "scanpy": mock_scanpy,
        "scanpy.pp": mock_scanpy.pp,
        "scanpy.pl": mock_scanpy.pl,
        "scanpy.tl": mock_scanpy.tl,
        "anndata": mock_anndata,
        "matplotlib": mock_matplotlib,
        "matplotlib.pyplot": mock_matplotlib_pyplot,
        "scipy": mock_scipy,
        "scipy.stats": mock_scipy_stats,
    }

    for name, mod in mods.items():
        sys.modules.setdefault(name, mod)

    return mods


_MOCKS = _install_mock_modules()

# Force (re)import of scanpy_qc now that mocks are in place
if "scanpy_qc" in sys.modules:
    del sys.modules["scanpy_qc"]
import scanpy_qc  # noqa: E402
from scanpy_qc import run_scanpy_qc  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies for random QC parameters
# ---------------------------------------------------------------------------

_species_strategy = st.sampled_from(["human", "mouse"])

_qc_params_strategy = st.fixed_dictionaries(
    {
        "species": _species_strategy,
    },
    optional={
        "mad_counts": st.floats(min_value=1.0, max_value=10.0, allow_nan=False),
        "mad_genes": st.floats(min_value=1.0, max_value=10.0, allow_nan=False),
        "mad_mt": st.floats(min_value=1.0, max_value=10.0, allow_nan=False),
        "mt_threshold": st.floats(min_value=1.0, max_value=50.0, allow_nan=False),
        "min_cells": st.integers(min_value=1, max_value=100),
    },
)


# ---------------------------------------------------------------------------
# Helpers to build a fake AnnData-like object
# ---------------------------------------------------------------------------

def _make_mock_adata(n_obs: int, n_vars: int, mt_prefix: str = "MT-"):
    """Build a minimal mock AnnData with the fields scanpy_qc.py reads."""
    rng = np.random.default_rng(42)

    # Gene names — first 5 are mitochondrial
    n_mt = min(5, n_vars)
    gene_names = [f"{mt_prefix}{i}" for i in range(n_mt)] + [
        f"GENE{i}" for i in range(n_vars - n_mt)
    ]

    # A thin wrapper around ndarray that adds .mean() and .median() so it
    # behaves like a pandas Series (which is what real AnnData .obs returns).
    class _SeriesLike(np.ndarray):
        """ndarray subclass with .median() for pandas-like API."""

        def median(self):
            return float(np.median(self))

    def _as_series(arr):
        return arr.view(_SeriesLike)

    # Obs data that calculate_qc_metrics would produce
    total_counts = _as_series(rng.integers(500, 20000, size=n_obs).astype(float))
    n_genes_by_counts = _as_series(rng.integers(200, n_vars, size=n_obs).astype(float))
    pct_counts_mt = _as_series(rng.uniform(0, 15, size=n_obs).astype(float))

    # Build a mock that behaves like AnnData
    adata = MagicMock()
    adata.n_obs = n_obs
    adata.n_vars = n_vars

    # var_names needs .str.startswith for MT detection
    var_names_mock = MagicMock()
    startswith_result = np.array(
        [name.startswith(mt_prefix) for name in gene_names]
    )
    var_names_mock.str.startswith.return_value = startswith_result
    adata.var_names = var_names_mock
    adata.var = {}

    # obs needs dict-like access returning numpy arrays
    obs_data = {
        "total_counts": total_counts,
        "n_genes_by_counts": n_genes_by_counts,
        "pct_counts_mt": pct_counts_mt,
    }

    class _FakeObs:
        def __init__(self, data):
            self._d = dict(data)

        def __getitem__(self, key):
            return self._d[key]

        def __setitem__(self, key, value):
            self._d[key] = value

        def __contains__(self, key):
            return key in self._d

    adata.obs = _FakeObs(obs_data)

    # After filtering the mock adata is sliced — simulate reduced counts
    filtered_n_obs = max(1, int(n_obs * 0.8))
    filtered_adata = MagicMock()
    filtered_adata.n_obs = filtered_n_obs
    filtered_adata.n_vars = n_vars
    filtered_adata.obs = _FakeObs({
        "total_counts": _as_series(total_counts[:filtered_n_obs].copy()),
        "n_genes_by_counts": _as_series(n_genes_by_counts[:filtered_n_obs].copy()),
        "pct_counts_mt": _as_series(pct_counts_mt[:filtered_n_obs].copy()),
    })
    filtered_adata.write_h5ad = MagicMock()

    # adata[bool_mask, :] → object whose .copy() returns filtered_adata
    slice_result = MagicMock()
    slice_result.copy.return_value = filtered_adata
    adata.__getitem__ = MagicMock(return_value=slice_result)

    return adata, filtered_adata


def _patch_scanpy_qc_deps(mock_adata):
    """
    Patch the module-level names inside the already-imported scanpy_qc so
    that run_scanpy_qc() uses our mocks instead of real scanpy/anndata.
    """
    mock_ad = MagicMock()
    mock_ad.read_h5ad.return_value = mock_adata

    mock_sc = MagicMock()
    mock_sc.pp.calculate_qc_metrics = MagicMock()
    mock_sc.pp.filter_cells = MagicMock()
    mock_sc.pp.filter_genes = MagicMock()
    mock_sc.pl.violin = MagicMock()

    mock_plt = MagicMock()
    mock_fig = MagicMock()
    mock_ax = MagicMock()
    mock_plt.subplots.return_value = (mock_fig, mock_ax)

    mock_mad_fn = MagicMock(return_value=1000.0)

    patches = {
        "ad": mock_ad,
        "sc": mock_sc,
        "plt": mock_plt,
        "median_abs_deviation": mock_mad_fn,
    }
    return patches


# ---------------------------------------------------------------------------
# Property 2: QC response structure completeness
# ---------------------------------------------------------------------------

@settings(max_examples=50, deadline=None)
@given(params=_qc_params_strategy)
def test_qc_response_structure_completeness(params):
    """
    For any valid QC parameter combination, run_scanpy_qc must return a dict
    containing:
      - qc_summary.before_filtering with n_cells, n_genes,
        mean_counts_per_cell, median_counts_per_cell, mean_mt_pct
      - qc_summary.after_filtering with n_cells, n_genes, cells_removed,
        cells_retained_pct
      - plots: a list of exactly 3 filenames
      - filtered_h5ad: a non-empty string
    """
    mt_prefix = "MT-" if params.get("species", "human") == "human" else "mt-"
    mock_adata, _ = _make_mock_adata(100, 500, mt_prefix=mt_prefix)
    dep_patches = _patch_scanpy_qc_deps(mock_adata)

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_h5ad = os.path.join(tmpdir, "input.h5ad")
        with open(fake_h5ad, "wb") as f:
            f.write(b"\x00")

        # Monkey-patch module-level names in scanpy_qc
        originals = {}
        for attr, mock_obj in dep_patches.items():
            originals[attr] = getattr(scanpy_qc, attr)
            setattr(scanpy_qc, attr, mock_obj)

        try:
            result = run_scanpy_qc(fake_h5ad, params, tmpdir)
        finally:
            for attr, orig in originals.items():
                setattr(scanpy_qc, attr, orig)

        # ---- Assert top-level keys ----
        assert "qc_summary" in result, "Missing 'qc_summary' key"
        assert "plots" in result, "Missing 'plots' key"
        assert "filtered_h5ad" in result, "Missing 'filtered_h5ad' key"

        # ---- Assert before_filtering structure ----
        qc = result["qc_summary"]
        assert "before_filtering" in qc, "Missing 'before_filtering'"
        before = qc["before_filtering"]
        before_required = {
            "n_cells", "n_genes", "mean_counts_per_cell",
            "median_counts_per_cell", "mean_mt_pct",
        }
        assert before_required.issubset(before.keys()), (
            f"before_filtering missing keys: {before_required - before.keys()}"
        )

        # ---- Assert after_filtering structure ----
        assert "after_filtering" in qc, "Missing 'after_filtering'"
        after = qc["after_filtering"]
        after_required = {
            "n_cells", "n_genes", "cells_removed", "cells_retained_pct",
        }
        assert after_required.issubset(after.keys()), (
            f"after_filtering missing keys: {after_required - after.keys()}"
        )

        # ---- Assert numeric types ----
        for key in ("n_cells", "n_genes"):
            assert isinstance(before[key], (int, float)), (
                f"before[{key}] should be numeric, got {type(before[key])}"
            )
            assert isinstance(after[key], (int, float)), (
                f"after[{key}] should be numeric, got {type(after[key])}"
            )
        assert isinstance(after["cells_removed"], (int, float))
        assert isinstance(after["cells_retained_pct"], (int, float))

        # ---- Assert plots list ----
        assert isinstance(result["plots"], list), "plots should be a list"
        assert len(result["plots"]) == 3, (
            f"Expected exactly 3 plots, got {len(result['plots'])}"
        )

        # ---- Assert filtered_h5ad ----
        assert isinstance(result["filtered_h5ad"], str)
        assert len(result["filtered_h5ad"]) > 0, "filtered_h5ad must be non-empty"


@settings(max_examples=30, deadline=None)
@given(params=_qc_params_strategy)
def test_qc_cells_removed_is_consistent(params):
    """
    cells_removed must equal before.n_cells - after.n_cells, and
    cells_retained_pct must be in [0, 100].
    """
    mt_prefix = "MT-" if params.get("species", "human") == "human" else "mt-"
    mock_adata, _ = _make_mock_adata(100, 500, mt_prefix=mt_prefix)
    dep_patches = _patch_scanpy_qc_deps(mock_adata)

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_h5ad = os.path.join(tmpdir, "input.h5ad")
        with open(fake_h5ad, "wb") as f:
            f.write(b"\x00")

        originals = {}
        for attr, mock_obj in dep_patches.items():
            originals[attr] = getattr(scanpy_qc, attr)
            setattr(scanpy_qc, attr, mock_obj)

        try:
            result = run_scanpy_qc(fake_h5ad, params, tmpdir)
        finally:
            for attr, orig in originals.items():
                setattr(scanpy_qc, attr, orig)

        before = result["qc_summary"]["before_filtering"]
        after = result["qc_summary"]["after_filtering"]

        assert after["cells_removed"] == before["n_cells"] - after["n_cells"], (
            f"cells_removed ({after['cells_removed']}) != "
            f"before ({before['n_cells']}) - after ({after['n_cells']})"
        )
        assert 0 <= after["cells_retained_pct"] <= 100, (
            f"cells_retained_pct out of range: {after['cells_retained_pct']}"
        )


@settings(max_examples=30, deadline=None)
@given(params=_qc_params_strategy)
def test_qc_plot_filenames_are_expected(params):
    """
    The three plot filenames must be the exact expected names.
    """
    mt_prefix = "MT-" if params.get("species", "human") == "human" else "mt-"
    mock_adata, _ = _make_mock_adata(100, 500, mt_prefix=mt_prefix)
    dep_patches = _patch_scanpy_qc_deps(mock_adata)

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_h5ad = os.path.join(tmpdir, "input.h5ad")
        with open(fake_h5ad, "wb") as f:
            f.write(b"\x00")

        originals = {}
        for attr, mock_obj in dep_patches.items():
            originals[attr] = getattr(scanpy_qc, attr)
            setattr(scanpy_qc, attr, mock_obj)

        try:
            result = run_scanpy_qc(fake_h5ad, params, tmpdir)
        finally:
            for attr, orig in originals.items():
                setattr(scanpy_qc, attr, orig)

        expected_plots = [
            "qc_violin_before_filtering.png",
            "qc_thresholds.png",
            "qc_violin_after_filtering.png",
        ]
        assert result["plots"] == expected_plots, (
            f"Unexpected plot names: {result['plots']}"
        )
