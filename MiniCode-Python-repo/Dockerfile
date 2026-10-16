# =============================================================================
# MiniCode Python — Multi-stage Dockerfile
# =============================================================================
# Supported deployment modes:
#   - Interactive CLI container for local/SSH use
#   - Headless one-shot container for CI and automation
#
# Quick start:
#   docker build -t minicode-py .
#   docker run -it --rm \
#     -e MINI_CODE_PROVIDER=deepseek \
#     -e MINI_CODE_MODEL=deepseek-v4-pro \
#     -e DEEPSEEK_API_KEY=sk-... \
#     -v $(pwd):/workspace \
#     minicode-py
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder — install package into venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build
ARG MINICODE_INSTALL_TARGET=.

# Copy package source
COPY pyproject.toml README.md ./
COPY minicode/ ./minicode/
COPY Main/ ./Main/
COPY Package/ ./Package/

# Install into a clean venv (keeps final image small)
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/minicode-venv && \
    /opt/minicode-venv/bin/pip install --upgrade pip && \
    /opt/minicode-venv/bin/pip install "${MINICODE_INSTALL_TARGET}"

# ---------------------------------------------------------------------------
# Stage 2: Runtime — venv and native search dependency
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="MiniCode Python"
LABEL org.opencontainers.image.description="A lightweight terminal coding assistant — the agent that grows with you"
LABEL org.opencontainers.image.source="https://github.com/QUSETIONS/MiniCode-Python"

# grep_files uses the native executable, including PCRE2 for advanced patterns.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ripgrep && \
    rg --pcre2-version && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd --gid 1000 minicode && \
    useradd --uid 1000 --gid minicode --create-home --shell /bin/bash minicode

# Copy venv from builder
COPY --from=builder /opt/minicode-venv /opt/minicode-venv

# Make minicode-py available on PATH
ENV PATH="/opt/minicode-venv/bin:${PATH}"

# Create persistent data directories
RUN mkdir -p \
        /home/minicode/.mini-code/memory \
        /home/minicode/.mini-code/memory-v2/artifacts \
        /home/minicode/.mini-code/memory-v2/runtime-artifacts \
        /home/minicode/.mini-code/memory-v2/spool \
        /home/minicode/.mini-code/memory-v2/backups \
        /home/minicode/.mini-code/skills && \
    chown -R minicode:minicode /home/minicode/.mini-code

# Default workspace
RUN mkdir -p /workspace && chown minicode:minicode /workspace
WORKDIR /workspace

# Environment defaults (override at runtime)
ENV MINI_CODE_LOG_LEVEL=WARNING \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    # Container hint — lets MiniCode know it's running in Docker
    MINI_CODE_CONTAINER=docker

# Health check: verify the CLI entry point works
HEALTHCHECK --interval=60s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from minicode.main import main; print('ok')" || exit 1

# Switch to non-root user
USER minicode

# Default entry: interactive CLI mode
ENTRYPOINT ["minicode-py"]
