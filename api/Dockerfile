# syntax=docker/dockerfile:1
# Kochi Healthcare WebGIS - API + MapLibre client
#
# Multi-stage build (Q10, see study/S4.md):
#   stage "builder"  python:3.12-slim + gcc/headers. Compiles every dependency
#                    into a wheel (/wheels). Big, never shipped.
#   stage "runtime"  python:3.12-slim only. Installs the pre-built wheels
#                    offline, adds the app + client, runs as a non-root user.
#
# Build context is the repository root:
#   docker build -f api/Dockerfile --target builder -t kochi-healthcare-api:builder .
#   docker build -f api/Dockerfile --target runtime -t kochi-healthcare-api:latest .
# (docker compose builds the default last stage = runtime.)

ARG PYTHON_IMAGE=python:3.12-slim

# ---------------------------------------------------------------- builder ----
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# Compilers are only needed to build wheels from source distributions.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# Copy only the requirements first: this layer (and the wheel build below) is
# cached until requirements.txt changes, so code edits rebuild in seconds.
COPY api/requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------- runtime ----
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="kochi-healthcare-api" \
      org.opencontainers.image.description="FastAPI + pgRouting walking-access API and MapLibre client (Kochi Healthcare WebGIS)" \
      org.opencontainers.image.source="https://github.com/adilabd123/Kochi-healthcare-webgis"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLIENT_DIR=/app/client
RUN groupadd --gid 10001 appuser \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser
WORKDIR /app
# Install from the builder's wheels only (no compilers, no internet needed);
# the bind mount keeps /wheels out of the final image layers.
COPY api/requirements.txt /tmp/requirements.txt
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt
COPY --chown=appuser:appuser api/app ./app
COPY --chown=appuser:appuser client ./client
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
