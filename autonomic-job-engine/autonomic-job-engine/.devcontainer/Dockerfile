# Development container: Python 3.12 + uv (Docker-in-Docker is added via devcontainer feature)
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get -y update \
 && apt-get -y upgrade \
 && apt-get -y install --no-install-recommends git curl ca-certificates \
 && apt-get -y clean \
 && rm -rf /var/lib/apt/lists/*

# uv / uvx
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /usr/local/bin/
