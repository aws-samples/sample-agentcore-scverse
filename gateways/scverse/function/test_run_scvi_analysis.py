"""Tests for the run_scvi_analysis gateway tool (model selection + validation)."""

from run_scvi_analysis import run_scvi_analysis


class TestModelValidation:
    def test_default_scvi(self):
        result = run_scvi_analysis({})
        assert result["status"] == "success"
        assert "scVI" in result["parameters"]["model"]
        assert result["training_status"] == "validated"

    def test_unknown_model(self):
        result = run_scvi_analysis({"model": "nonexistent"})
        assert result["status"] == "error"
        assert "Unknown model" in result["error"]

    def test_scanvi_requires_labels(self):
        result = run_scvi_analysis({"model": "scanvi"})
        assert result["status"] == "error"
        assert "labels_key" in result["error"]

    def test_scanvi_with_labels(self):
        result = run_scvi_analysis({"model": "scanvi", "labels_key": "cell_type"})
        assert result["status"] == "success"

    def test_totalvi_requires_protein(self):
        result = run_scvi_analysis({"model": "totalvi"})
        assert result["status"] == "error"
        assert "protein_expression" in result["error"]

    def test_destvi_requires_spatial_and_reference(self):
        result = run_scvi_analysis({"model": "destvi"})
        assert result["status"] == "error"
        assert "spatial_data" in result["error"]

    def test_peakvi_validates(self):
        result = run_scvi_analysis({"model": "peakvi"})
        assert result["status"] == "success"
        assert result["training_status"] == "validated"

    def test_case_insensitive_model(self):
        result = run_scvi_analysis({"model": "SCVI"})
        assert result["status"] == "success"


class TestDataTypeCompatibility:
    def test_scvi_rejects_cite_seq(self):
        result = run_scvi_analysis({"model": "scvi", "data_type": "cite-seq"})
        assert result["status"] == "error"
        assert "totalVI" in result.get("recommendation", "")

    def test_peakvi_rejects_scrna(self):
        result = run_scvi_analysis({"model": "peakvi", "data_type": "scrna-seq"})
        assert result["status"] == "error"
        assert "scVI" in result.get("recommendation", "")

    def test_scvi_accepts_scrna(self):
        """scVI should accept scRNA-seq data (no incompatibility)."""
        result = run_scvi_analysis({"model": "scvi", "data_type": "scrna-seq"})
        assert result["status"] == "success"

    def test_no_data_type_skips_check(self):
        """If data_type is not provided, skip the compatibility check."""
        result = run_scvi_analysis({"model": "scvi"})
        assert result["status"] == "success"
