FROM python:3.15.0-slim@sha256:2da676046630d7bf14c6764ae1143dc7fa0377a53f94bf5a642f0b214aca5bce AS builder
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.4 \
    && UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --all-extras --no-editable

FROM python:3.15.0-slim@sha256:2da676046630d7bf14c6764ae1143dc7fa0377a53f94bf5a642f0b214aca5bce AS runtime
RUN groupadd --gid 10001 memory && useradd --uid 10001 --gid memory --create-home memory \
    && mkdir /data && chown memory:memory /data
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
USER memory
ENV MEMORY_WORLDS_DATA_DIR=/data
ENV PATH="/opt/venv/bin:$PATH"
ENV GRAPHITI_TELEMETRY_ENABLED=false
EXPOSE 8787
ENTRYPOINT ["memory-worlds"]
CMD ["serve", "--host", "0.0.0.0", "--allow-network"]
