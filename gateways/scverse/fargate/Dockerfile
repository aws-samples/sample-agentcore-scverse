FROM public.ecr.aws/docker/library/python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ libhdf5-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Use uv — a Rust-based pip replacement. On this workload it converges in
# seconds where pip's resolver backtracks for 10+ minutes through the
# scverse dep tree (scvi-tools/squidpy/pertpy all pull loose JAX/xarray bounds).
RUN pip install --no-cache-dir uv==0.11.31

COPY requirements.txt .

# Install everything in one uv invocation. uv has a modern PubGrub-based
# resolver that handles this dep graph in <60s. CPU-only torch is pulled
# from the pytorch CPU index.
ENV UV_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN uv pip install --system --no-cache --index-strategy unsafe-best-match -r requirements.txt

COPY fargate_worker.py .
COPY workflows/ ./workflows/

RUN useradd -m -u 1000 worker
USER worker

ENTRYPOINT ["python", "fargate_worker.py"]
