# Cloud Run API image (also used as Dockerfile.api).
# Chromium-only Playwright for website MCP. No frontend/terraform in the context.

FROM python:3.11-slim-bookworm

# Cloud Build passes its built-in COMMIT_SHA; local builds default to unknown.
ARG COMMIT_SHA=unknown
ENV COMMIT_SHA=${COMMIT_SHA}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    shared-mime-info \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Chromium only (not Firefox/WebKit). install-deps pulls the Debian packages
# Playwright needs; keep PLAYWRIGHT_BROWSERS_PATH so Cloud Run finds the browser.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY backend ./backend
COPY main.py ./main.py
COPY scripts/verify_deployed_workflows.py ./scripts/verify_deployed_workflows.py

# STORAGE_BACKEND is not baked in. Local docker defaults to sqlite via Settings.
# Cloud Run production must set STORAGE_BACKEND=firestore (and GOOGLE_CLOUD_PROJECT).
# Do not set GEMINI_API_KEY. Vertex uses ADC / the Cloud Run service account.
ENV PYTHONPATH=/app \
    PORT=8080 \
    APP_ENV=production \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Cloud Run sets PORT. Do not bake .env into this image.
# Run the API/worker and browser as an unprivileged identity.
# The repo tree may carry restrictive modes (700/600); the unprivileged
# runtime user must be able to read every copied file.
RUN groupadd --gid 10001 agentos && useradd --uid 10001 --gid 10001 --create-home agentos \
    && mkdir -p /app/data \
    && chown -R agentos:agentos /app/data \
    && chmod -R a+rX /ms-playwright
# Copy preserves source modes — normalize so uid 10001 can read everything.
RUN chmod -R a+rX /app
# Dev-mode containers auto-generate JWT keypairs here; production
# injects them from Secret Manager instead.
RUN mkdir -p /app/backend/security/keys && chown 10001:10001 /app/backend/security/keys /app/data

USER 10001:10001

CMD ["sh", "-c", "uvicorn backend.api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
