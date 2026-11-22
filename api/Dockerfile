# Kochi Healthcare WebGIS - API + static Leaflet client
# Multi-stage build (see study/S4.md):
#   stage 1 "builder" installs Python packages into /install
#   stage 2 "runtime" copies only those packages and the app code
# Build context is the repository root:
#   docker build -f api/Dockerfile -t kochi-healthcare-api .

# ---------- stage 1: dependencies ----------
FROM python:3.12-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Copy requirements first: this layer is reused until requirements.txt changes.
COPY api/requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---------- stage 2: runtime ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLIENT_DIR=/app/client
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY api/app ./app
COPY client ./client
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
