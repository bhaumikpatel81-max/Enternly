FROM python:3.12-slim

WORKDIR /app

# System packages: LibreOffice headless for .doc → .docx conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so this layer caches until requirements change.
# Build context is the project root, so paths are relative to it.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source code
COPY backend/. .

# Copy frontend so the backend can serve index.html and assets at runtime
COPY frontend/ frontend/

# Non-root user (required by the deployment quality gate).
# Pre-create runtime-writable dirs (uploads, CV storage, avatar media, docs)
# and hand the whole app tree to appuser — several routers os.makedirs() into
# /app at import/request time (proctoring uploads, cv_store, avatar media),
# which fails under a non-root user if /app is still root-owned.
RUN useradd -m appuser \
    && mkdir -p /app/proctoring_uploads /app/cv_store /app/cv_inbox \
    && chown -R appuser:appuser /app
USER appuser

# App binds to 0.0.0.0 and reads PORT from the environment (not hard-coded)
ENV PORT=8080
# Absolute path to the bundled frontend inside the container.
# main.py reads this env var so the path is always correct regardless of
# how Python resolves __file__ relative paths.
ENV FRONTEND_DIR=/app/frontend
EXPOSE 8080

# Healthcheck (required by the deployment quality gate)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8080\")}/api/health')" || exit 1

# Worker count is configurable (WEB_CONCURRENCY) so free-tier deployments
# (Render Free, Neon Free's low connection ceiling) aren't forced into
# workers × DB_POOL_MAX connections far beyond what the DB can accept.
# Default of 2 is deliberately conservative; raise it via env on bigger infra.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers=${WEB_CONCURRENCY:-2}"]
