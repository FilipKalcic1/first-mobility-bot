# =============================================================================
# MobilityOne WhatsApp Bot - Optimized Production Dockerfile
# Version: 12.0 - Multi-stage build, <500MB target
#
# OPTIMIZATIONS:
# - Multi-stage build: Builder (compile) + Runtime (minimal)
# - No docker.io in main image (uses Docker SDK in autoscaler)
# - Removed build-essential from runtime
# - Optimized layer caching
# - Target: <400MB (API/Worker), <450MB (with full deps)
# =============================================================================

# =============================================================================
# STAGE 1: BUILDER - Compile dependencies
# =============================================================================
FROM python:3.12-slim AS builder

# Build environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy and install Python dependencies
COPY requirements.txt .

# Create virtual environment and install deps
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# =============================================================================
# STAGE 2: RUNTIME - Minimal production image
# =============================================================================
FROM python:3.12-slim AS runtime

# Runtime environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# Install ONLY runtime dependencies (no build tools!)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && rm -rf /var/cache/apt/*

WORKDIR /app

# Create non-root user
RUN groupadd -r appgroup && useradd -r -g appgroup appuser && \
    mkdir -p /app/.cache && \
    chown -R appuser:appgroup /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application code
COPY --chown=appuser:appgroup . .

# Make init script executable if exists
RUN chmod +x /app/docker/init-db.sh 2>/dev/null || true

# ML model training removed in Phase D (ML cull).

# Switch to non-root user
USER appuser

# Healthcheck (uses /ready for full dependency check)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/ready || exit 1

# Entrypoint with tini for proper signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command (API)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
