# Address Geocoding Application
# Dockerfile for containerized deployment

# Debian 12 (bookworm) base to match Microsoft's debian/12 repo for
# msodbcsql18. To pin it, append the index digest from
#   docker buildx imagetools inspect python:3.13-slim-bookworm
# as  FROM python:3.13-slim-bookworm@sha256:<digest>  and bump it monthly.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONSOLE_FORMAT=json \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Microsoft's signing-key fingerprint. When set (from Microsoft's docs),
# the build fails unless the downloaded key matches it.
ARG MS_KEY_FINGERPRINT=""

# One layer: runtime ODBC (unixodbc) + Microsoft ODBC Driver 18. curl and
# gnupg are needed only to add the repo and are purged in the same layer.
# unixodbc-dev is not installed: nothing is compiled (pip uses wheels only).
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg unixodbc; \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc -o /tmp/microsoft.asc; \
    if [ -n "$MS_KEY_FINGERPRINT" ]; then \
        gpg --show-keys --with-colons /tmp/microsoft.asc \
            | awk -F: '/^fpr/ {print $10}' | grep -qx "$MS_KEY_FINGERPRINT" \
            || { echo "Microsoft key fingerprint mismatch" >&2; exit 1; }; \
    fi; \
    gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg /tmp/microsoft.asc; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list; \
    apt-get update; \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/* /tmp/microsoft.asc

# Non-root runtime user. App code stays root-owned (read-only to the job);
# only the log directory is writable.
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin appuser \
    && install -d -o appuser -g appuser /app/logs

# Vendored app_logger (not on PyPI), named exactly so a stray wheel in
# vendor/ is never installed. It has no runtime dependencies.
COPY vendor/app_logger-2.1.1-py3-none-any.whl /tmp/
COPY requirements.txt .

# --only-binary: fail the build instead of compiling from source.
RUN pip install --no-cache-dir --no-deps /tmp/app_logger-2.1.1-py3-none-any.whl \
    && pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && rm -f /tmp/app_logger-2.1.1-py3-none-any.whl

COPY GeocodeAddresses.py .

USER appuser

# Exec form so SIGTERM reaches Python for graceful shutdown.
CMD ["python", "GeocodeAddresses.py"]
