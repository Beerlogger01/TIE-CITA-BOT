# syntax=docker/dockerfile:1
# Supervisor must not replace Debian with its legacy Alpine BUILD_FROM fallback.
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ARG BUILD_VERSION=0.3.0
LABEL io.hass.type="app" io.hass.version="${BUILD_VERSION}"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright HOME=/home/cita
WORKDIR /app
COPY requirements.lock /app/requirements.lock
COPY src /app/src
COPY --chmod=755 run.sh /app/run.sh
COPY install_novnc.py /app/install_novnc.py
# One installation layer also keeps VFS-based local builders within their disk budget.
# A proxy CA is optional, mounted only for this build step and removed before commit.
RUN --mount=type=secret,id=proxy_ca \
    set -eu; \
    if [ -s /run/secrets/proxy_ca ]; then \
      cp /run/secrets/proxy_ca /usr/local/share/ca-certificates/build-proxy.crt; \
      update-ca-certificates; \
      export PIP_CERT=/run/secrets/proxy_ca NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca SSL_CERT_FILE=/run/secrets/proxy_ca; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc websockify openbox tini gosu ca-certificates tzdata fonts-dejavu-core \
    && python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m playwright install --with-deps chromium --no-shell \
    && python /app/install_novnc.py \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin cita \
    && chmod -R a+rX /opt/playwright \
    && chmod -R a+rX /app/src /usr/share/novnc \
    && dpkg-query -W > /app/system-packages.txt \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/local/share/ca-certificates/build-proxy.crt && update-ca-certificates
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/run.sh"]
