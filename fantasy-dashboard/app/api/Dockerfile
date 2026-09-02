# ── Fantasy Dashboard — Python API ──────────────────────────
# Build context: fantasy-dashboard/app/api/
# Entrypoint:   uvicorn main:app --host 0.0.0.0 --port 8000
# ─────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

# libpq5 required by psycopg[binary]
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Dependency layer (cached until requirements.txt changes) ──
FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Final image ───────────────────────────────────────────────
FROM deps AS final

COPY . .

# Non-root runtime user
RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
