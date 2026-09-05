FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG APP_VERSION=0.5.2
ARG BUILD_DATE=unknown
ARG VCS_REF=unknown
ARG VCS_URL=local://penczreq

LABEL org.opencontainers.image.title="penczREQ" \
      org.opencontainers.image.description="Prywatny system requestów multimedialnych dla Jellyfin" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="${VCS_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    TZ=Europe/Warsaw

RUN groupadd --system --gid 568 penczreq \
    && useradd --system --uid 568 --gid 568 --home-dir /nonexistent \
       --shell /usr/sbin/nologin penczreq

WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements.lock \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip \
              /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12
COPY --chown=568:568 request_app ./request_app

USER 568:568
EXPOSE 8000 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/internal/health', timeout=3)"]
CMD ["python", "-m", "request_app.server", "public", "--host", "0.0.0.0", "--port", "8000"]
