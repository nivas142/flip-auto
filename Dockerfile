FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLIP_AUTO_EXECUTION_MODE=shadow
WORKDIR /app

COPY requirements.txt requirements-gcp.txt ./
RUN pip install --no-cache-dir -r requirements-gcp.txt \
    && groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid monitor --no-create-home monitor

# Explicit allowlist: never copy local credentials, config.yaml, email samples,
# report PDFs, state snapshots, or the Git checkout into an image layer.
COPY --chown=10001:10001 monitor.py cloud_cma.py cloud_cma_callback.py \
    deal_screening.py valuation.py gcp_runtime.py config.example.yaml ./
USER 10001:10001
ENTRYPOINT ["python", "gcp_runtime.py"]
