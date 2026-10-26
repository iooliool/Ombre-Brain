# ============================================================
# Ombre-Brain on Render (free web service, no persistent disk)
#
# Memories live in a private GitHub repository, not on this container's disk:
# src/miles_cloud.py restores them at boot and pushes every change back.
# The original upstream Dockerfile is kept as Dockerfile.upstream.
#
# Render settings: Language = Docker, Instance Type = Free.
# Required env: OMBRE_GITHUB_TOKEN, MILES_MEMORY_REPO, OMBRE_DASHBOARD_PASSWORD
# (full list: deploy/render/README.md)
# ============================================================
FROM python:3.12-slim

WORKDIR /app

ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
        ${PIP_INDEX_URL:+-i "$PIP_INDEX_URL"} \
        ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
        --require-hashes -r requirements.lock.txt

COPY src/ ./src/
COPY frontend/ ./frontend/
COPY VERSION ./VERSION
COPY config.example.yaml ./config.default.yaml
COPY tools/ ./tools/
COPY kernel/ ./kernel/
COPY docs/CLAUDE_PROMPT.md docs/ENVIRONMENT_VARIABLES.md docs/INTERNALS.md docs/MULTI_OWNER.md docs/OPERATIONS.md ./docs/
COPY docs/adr/ ./docs/adr/
COPY README.md ./README.md
COPY CHANGELOG.md ./CHANGELOG.md

# Precompile bytecode so a cold start after Render's sleep spends less of the
# free instance's small CPU share on parsing Python.
RUN python -m compileall -q /app/src /app/kernel || true

ENV PYTHONUNBUFFERED=1 \
    OMBRE_TRANSPORT=streamable-http \
    OMBRE_MCP_AUTH_MODE=oauth \
    OMBRE_BUCKETS_DIR=/app/buckets \
    OMBRE_CONFIG_PATH=/app/runtime/config.yaml \
    OMBRE_EMBED_BACKEND=api \
    AI_NAME=Miles \
    OMBRE_TRUSTED_PROXY_CIDRS=10.0.0.0/8,127.0.0.0/8,::1/128

# Render passes PORT (default 10000); miles_cloud.py maps it to OMBRE_PORT.
EXPOSE 10000

CMD ["python", "src/miles_cloud.py"]
