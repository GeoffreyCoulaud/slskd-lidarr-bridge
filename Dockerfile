# ---- builder ----
FROM python:3.13-slim AS builder

# Install uv from the official image, pinned to the version that resolved
# uv.lock so `uv sync --frozen` is byte-for-byte reproducible
COPY --from=ghcr.io/astral-sh/uv:0.8.11 /uv /bin/uv

WORKDIR /app

# Copy dependency manifests and source; sync into /app/.venv
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# --no-dev: skip dev dependencies; --frozen: use exact uv.lock pins
# uv sync also installs the project package itself (via hatchling)
RUN uv sync --no-dev --frozen

# ---- runtime ----
FROM python:3.13-slim

# Copy the entire /app tree (venv + installed project source) from builder
COPY --from=builder /app /app

# Persistent data directory for the SQLite database
# chown to nobody (65534) so the non-root user can write the DB file
RUN mkdir -p /data && chown 65534:65534 /data

EXPOSE 8765

# Default database path; override with BRIDGE_DB_PATH if desired
ENV BRIDGE_DB_PATH=/data/bridge.db

# Healthcheck uses venv python (no curl/wget in slim); reads BRIDGE_PORT at runtime
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["/app/.venv/bin/python","-c","import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('BRIDGE_PORT','8765'))"]

# Run as nobody (UID/GID 65534) — least-privilege
USER nobody

CMD ["/app/.venv/bin/python", "-m", "slskd_lidarr_bridge.main"]
