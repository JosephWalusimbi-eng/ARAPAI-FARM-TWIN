# FARM TWIN server: FastAPI + the offline-first app + the phone simulator.
# The committed data snapshot (data/snapshot/) seeds the cache at startup, so the container
# needs no NASA downloads and no Earthdata token to run the demo.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FARM_TWIN_POWER_SOURCE=s3 PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY scripts/ scripts/
COPY data/snapshot/ data/snapshot/

# Run as a normal user; the app writes only to data/cache, data/plans and data/channels.
RUN useradd --create-home farmtwin && mkdir -p data/cache data/plans data/channels \
    && chown -R farmtwin /app
USER farmtwin

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"
CMD ["sh", "-c", "python -m uvicorn farm_twin.api:app --app-dir backend --host 0.0.0.0 --port ${PORT}"]
