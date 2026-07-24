"""
scvi-tools model-selection handler for the scverse Gateway.

Validates a model choice against the scvi-tools decision tree: it checks the
model name, required parameters, and data-type compatibility, then returns the
validated configuration. Live scvi-tools compute is dispatched asynchronously
(see ``submit_scverse_job`` / the Fargate worker) when an input dataset is
provided; this handler covers the model-selection reasoning path.
"""

import time
import uuid

SUPPORTED_MODELS = {
    "scvi": {
        "display_name": "scVI", "data_type": "scRNA-seq",
        "description": "Unsupervised integration, dimensionality reduction, DE",
        "required_params": [],
        "optional_params": ["batch_key", "n_hvgs", "n_latent", "max_epochs"],
    },
    "scanvi": {
        "display_name": "scANVI", "data_type": "scRNA-seq + cell type labels",
        "description": "Semi-supervised integration with label transfer",
        "required_params": ["labels_key"],
        "optional_params": ["batch_key", "n_hvgs", "n_latent", "max_epochs"],
    },
    "totalvi": {
        "display_name": "totalVI", "data_type": "CITE-seq (RNA + protein)",
        "description": "Multi-modal integration for CITE-seq data",
        "required_params": ["protein_expression"],
        "optional_params": ["batch_key", "n_hvgs", "n_latent", "max_epochs"],
    },
    "peakvi": {
        "display_name": "PeakVI", "data_type": "scATAC-seq",
        "description": "Chromatin accessibility analysis",
        "required_params": [],
        "optional_params": ["batch_key", "n_latent", "max_epochs"],
    },
    "multivi": {
        "display_name": "MultiVI", "data_type": "Multiome (RNA + ATAC)",
        "description": "Joint RNA and ATAC-seq analysis",
        "required_params": [],
        "optional_params": ["batch_key", "n_latent", "max_epochs"],
    },
    "destvi": {
        "display_name": "DestVI", "data_type": "Spatial + scRNA reference",
        "description": "Cell type deconvolution of spatial data",
        "required_params": ["spatial_data", "sc_reference"],
        "optional_params": ["n_latent", "max_epochs"],
    },
    "velovi": {
        "display_name": "veloVI", "data_type": "RNA velocity (spliced + unspliced)",
        "description": "Transcriptional dynamics modeling",
        "required_params": [],
        "optional_params": ["batch_key", "n_latent", "max_epochs"],
    },
}


def run_scvi_analysis(event: dict) -> dict:
    """Validate a scvi-tools model choice and return the selected configuration."""
    start_time = time.time()
    run_id = str(uuid.uuid4())[:8]
    model = event.get("model", "scvi")
    model_lower = model.lower().replace("-", "").replace("_", "")

    if model_lower not in SUPPORTED_MODELS:
        supported = ", ".join(f"{v['display_name']} ({k})" for k, v in SUPPORTED_MODELS.items())
        return {"status": "error", "tool": "run_scvi_analysis", "run_id": run_id,
                "error": f"Unknown model '{model}'. Supported: {supported}"}

    model_info = SUPPORTED_MODELS[model_lower]

    # Validate required params
    missing = [p for p in model_info["required_params"] if not event.get(p)]
    if missing:
        return {"status": "error", "tool": "run_scvi_analysis", "run_id": run_id,
                "model": model_info["display_name"],
                "error": f"{model_info['display_name']} requires: {', '.join(missing)}."}

    # Validate data type compatibility if specified
    data_type = event.get("data_type", "").lower()
    if data_type:
        incompatible = {
            "scvi": ["cite-seq", "atac-seq", "spatial", "multiome"],
            "scanvi": ["cite-seq", "atac-seq", "spatial", "multiome"],
            "totalvi": ["atac-seq", "spatial", "multiome"],
            "peakvi": ["cite-seq", "spatial", "scrna-seq", "rna-seq"],
            "multivi": ["cite-seq", "spatial"],
            "destvi": ["cite-seq", "atac-seq", "multiome"],
            "velovi": ["cite-seq", "atac-seq", "spatial", "multiome"],
        }
        suggestions = {
            "cite-seq": "totalVI", "atac-seq": "PeakVI", "spatial": "DestVI",
            "multiome": "MultiVI", "rna-seq": "scVI or scANVI", "scrna-seq": "scVI or scANVI",
        }
        for bad_type in incompatible.get(model_lower, []):
            if bad_type in data_type:
                suggested = suggestions.get(bad_type, "scVI")
                return {"status": "error", "tool": "run_scvi_analysis", "run_id": run_id,
                        "model": model_info["display_name"],
                        "error": f"{model_info['display_name']} is not designed for {data_type} data.",
                        "recommendation": f"For {data_type} data, consider using {suggested} instead."}

    params = {
        "model": model_info["display_name"], "data_type": model_info["data_type"],
        "n_hvgs": int(event.get("n_hvgs", 2000)),
        "n_latent": int(event.get("n_latent", 10)),
        "max_epochs": int(event.get("max_epochs", 50)),
    }
    if event.get("batch_key"):
        params["batch_key"] = event["batch_key"]

    return {
        "status": "success", "tool": "run_scvi_analysis", "run_id": run_id,
        "model": model_info["display_name"],
        "training_status": "validated",
        "processing_time_seconds": round(time.time() - start_time, 2),
        "parameters": params,
        "note": (
            f"{model_info['display_name']} selected and parameters validated for "
            f"{model_info['data_type']} data. Provide an input dataset to run live "
            "scvi-tools compute as an asynchronous job."
        ),
    }
