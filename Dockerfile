# ---- builder ----
# Alpine (musl) base: swapping glibc/coreutils/util-linux/perl for musl+busybox
# removes almost the entire Debian OS-package CVE surface (see SECURITY.md). The
# app is pure-Python + stdlib C modules (sqlite3/uuid/expat/zlib), so musl is a
# safe swap — validated by the full test suite running inside this image.
FROM python:3.14-alpine@sha256:26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92 AS builder

# Install uv from the official image, pinned to the version that resolved
# uv.lock so `uv sync --frozen` is byte-for-byte reproducible. The -alpine
# variant is musl-linked so the binary runs on this base (the default glibc
# image would not). uv installs to /usr/local/bin/uv there.
COPY --from=ghcr.io/astral-sh/uv:0.8.11-alpine@sha256:5231dffd04505a1ac25af87da8beb2a7c72afd0b1a4078b5cd97dc0724195e5c /usr/local/bin/uv /bin/uv

WORKDIR /app

# Copy dependency manifests and source; sync into /app/.venv
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# --no-dev: skip dev dependencies; --frozen: use exact uv.lock pins
# uv sync also installs the project package itself (via hatchling). All runtime
# deps ship musllinux wheels, so no compiler is needed in this stage.
RUN uv sync --no-dev --frozen

# ---- runtime ----
FROM python:3.14-alpine@sha256:26730869004e2b9c4b9ad09cab8625e81d256d1ce97e72df5520e806b1709f92

# Copy the entire /app tree (venv + installed project source) from builder
COPY --from=builder /app /app

# Persistent data directory for the SQLite database
# chown to nobody (65534) so the non-root user can write the DB file
RUN mkdir -p /data && chown 65534:65534 /data

EXPOSE 8765

# Default database path; override with BRIDGE_DB_PATH if desired
ENV BRIDGE_DB_PATH=/data/bridge.db

# Healthcheck uses venv python (no curl/wget dependency); reads BRIDGE_PORT at runtime
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["/app/.venv/bin/python","-c","import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('BRIDGE_PORT','8765'))"]

# Run as nobody (UID/GID 65534) — least-privilege
USER nobody

CMD ["/app/.venv/bin/python", "-m", "slskd_lidarr_bridge.main"]
