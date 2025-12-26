# syntax=docker/dockerfile:1.4
# Seedr-Sonarr Proxy Dockerfile
# Multi-stage build for smaller image size
# Supports: linux/amd64, linux/arm64

# =============================================================================
# Build stage
# =============================================================================
FROM --platform=$BUILDPLATFORM python:3.14-slim AS builder

# Build arguments
ARG TARGETPLATFORM
ARG BUILDPLATFORM
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION=1.0.0

WORKDIR /app

# Install build dependencies
# Note: gcc is needed for some Python packages with C extensions
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip wheel setuptools

# Copy pyproject.toml first for better layer caching
COPY pyproject.toml README.md ./

# Create a minimal __init__.py so pip install works for dependencies
RUN mkdir -p seedr_sonarr && echo '"""Seedr-Sonarr Proxy"""' > seedr_sonarr/__init__.py

# Install dependencies (this layer is cached if pyproject.toml doesn't change)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

# Now copy the actual source code
COPY seedr_sonarr/ seedr_sonarr/

# Reinstall to update with actual source (uses cached dependencies)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps .

# =============================================================================
# Runtime stage
# =============================================================================
FROM python:3.14-slim

# Build arguments for labels
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION=1.0.0

# OCI Image Format Specification labels
# https://github.com/opencontainers/image-spec/blob/main/annotations.md
LABEL org.opencontainers.image.title="Seedr-Sonarr Proxy" \
      org.opencontainers.image.description="qBittorrent API emulation for Seedr.cc - Use Seedr as a download client for Sonarr/Radarr" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="seedr-sonarr" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/example/seedr-sonarr" \
      org.opencontainers.image.documentation="https://github.com/example/seedr-sonarr#readme" \
      maintainer="Claude"

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 seedr

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application
COPY --from=builder /app/seedr_sonarr /app/seedr_sonarr

# Create directories with proper permissions
RUN mkdir -p /downloads /config && \
    chown -R seedr:seedr /downloads /config /app

# Environment variables with defaults
ENV HOST=0.0.0.0 \
    PORT=8080 \
    USERNAME=admin \
    PASSWORD=adminadmin \
    DOWNLOAD_PATH=/downloads \
    AUTO_DOWNLOAD=true \
    DELETE_AFTER_DOWNLOAD=true \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER seedr

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Default command
ENTRYPOINT ["seedr-sonarr"]
CMD ["serve"]