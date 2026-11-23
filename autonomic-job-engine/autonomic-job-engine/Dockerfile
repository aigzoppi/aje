# Autonomic Job Engine - API server
#
# Layout:  /app/aje/aje          the engine package (from src/aje)
#          /app/aje/aje_worker   the worker SDK (local backend workers, wire protocol, job context)
# Run from source with PYTHONPATH=/app/aje; dependencies come from pyproject.toml.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/aje \
    UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1

RUN apt-get -y update \
 && apt-get -y upgrade \
 && apt-get -y clean \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app/aje
# dependencies first: this layer is rebuilt only when pyproject.toml changes
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml
COPY src/aje ./aje
COPY src/aje_worker ./aje_worker

RUN useradd --uid 10001 --home-dir /home/aje --create-home aje \
 && mkdir -p /tmp/aje-workers \
 && chown -R aje /tmp/aje-workers
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status != 200)"

CMD ["uvicorn", "aje.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
