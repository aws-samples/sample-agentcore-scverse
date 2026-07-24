FROM public.ecr.aws/docker/library/python:3.11-slim
WORKDIR /app

# Runtime environment (single layer)
ENV PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    AWS_REGION=us-west-2 \
    AWS_DEFAULT_REGION=us-west-2

COPY requirements.txt requirements.txt
# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

RUN pip install --no-cache-dir aws-opentelemetry-distro==0.12.2

# Create non-root user
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore

EXPOSE 9000
EXPOSE 8000
EXPOSE 8080

# Copy entire project (respecting .dockerignore)
COPY . .

# Use the full module path
CMD ["opentelemetry-instrument", "python", "-m", "scverse_agent"]
