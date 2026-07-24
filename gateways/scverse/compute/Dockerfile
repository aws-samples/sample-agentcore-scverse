FROM public.ecr.aws/lambda/python:3.12

# Force cache bust on rebuild
ARG BUILD_DATE=unknown

# Numba and matplotlib need writable dirs. These ENV vars must be set
# BEFORE any Python import so they're available during Lambda init phase.
ENV NUMBA_CACHE_DIR=/tmp/numba_cache \
    NUMBA_DISABLE_JIT=1 \
    NUMBA_NUM_THREADS=1 \
    MPLCONFIGDIR=/tmp/matplotlib

RUN dnf install -y gcc gcc-c++ make pkgconfig && dnf clean all

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY compute_handler.py ${LAMBDA_TASK_ROOT}/
COPY scanpy_qc.py ${LAMBDA_TASK_ROOT}/
COPY scanpy_preprocessing.py ${LAMBDA_TASK_ROOT}/

CMD ["compute_handler.lambda_handler"]
