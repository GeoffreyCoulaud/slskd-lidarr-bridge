# ---- builder ----
FROM python:3.13-slim AS builder

# Install uv from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

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
RUN mkdir -p /data

EXPOSE 8765

# Default database path; override with DB_PATH if desired
ENV DB_PATH=/data/bridge.db

CMD ["/app/.venv/bin/python", "-m", "slskd_lidarr_bridge.main"]
