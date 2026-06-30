# AGENTS.md

Guidance for AI coding agents working in this repository. Almost all of the
code here is written by agents, so these conventions are the contract: follow
them, and leave the codebase as clean as you found it.

For *what the project does* and *how to deploy it*, read [README.md](README.md)
first. This file covers *how to work in the code*.

## What this is

A bridge that impersonates a **Newznab indexer** + **SABnzbd download client**
in front of [slskd](https://github.com/slsknet/slskd), so Lidarr drives
Soulseek through its normal Usenet workflow. Python 3.12+, Flask served by
waitress, SQLite for state, `httpx` to talk to slskd.

## Architecture

Hexagonal (ports & adapters). The dependency rule points inward: the domain
knows nothing about Flask, SQLite, or slskd.

```
src/slskd_lidarr_bridge/
├── domain/                 # pure business logic — no I/O, no framework imports
│   ├── ports.py            # Protocols: SoulseekGateway, ReleaseStore, JobStore, Clock
│   ├── models.py           # dataclasses: Release, DownloadJob, SearchResponse, …
│   ├── search_service.py   # search orchestration (uses ports only)
│   ├── download_service.py # download orchestration (uses ports only)
│   ├── paths.py            # storage-path computation (see multi-disc note below)
│   ├── quality.py, titles.py
├── adapters/
│   ├── inbound/            # driving adapters — Flask blueprints, XML/NZB wire formats
│   │   ├── app.py          # create_app(): wires services, registers blueprints
│   │   ├── newznab.py      # /indexer/* — Newznab caps/search XML
│   │   ├── sabnzbd.py      # /sabnzbd/* — SABnzbd JSON API
│   │   ├── nzb.py, xml.py  # self-describing NZB carrier + XML builders
│   └── outbound/           # driven adapters — implementations of the ports
│       ├── slskd_gateway.py   # SoulseekGateway over slskd's REST API (httpx)
│       ├── sqlite_store.py    # ReleaseStore + JobStore on SQLite
│       └── system_clock.py    # Clock
├── config.py               # Config.from_env() — all env parsing lives here
└── main.py                 # composition root: build_app(env) + waitress serve
```

Rules of thumb:
- **Domain depends only on ports**, never on a concrete adapter. New external
  capability → add a method to a `Protocol` in `ports.py`, then implement it in
  an outbound adapter.
- **Inbound adapters translate wire formats** (Newznab XML, SABnzbd JSON) to/from
  domain calls. Keep protocol quirks (e.g. Lidarr's contract) here, not in the
  domain.
- **All env parsing is in `config.py`.** Don't read `os.environ` elsewhere.

## Commands

Dependencies are managed with [uv](https://docs.astral.sh/uv/); the lockfile is
pinned. Run everything through `uv run`.

```bash
uv sync                      # install deps into .venv (use --frozen to match the lockfile exactly)
uv run ruff check .          # lint
uv run ruff format .         # format (CI runs `ruff format --check .`)
uv run mypy                  # strict type-check (config in pyproject.toml)
uv run pytest                # tests; enforces 100% coverage via fail_under
```

Before considering any change done, run **all four**: `ruff check`,
`ruff format --check`, `mypy`, and `pytest`. CI (`.github/workflows/checks.yml`)
runs the same gate and will block otherwise.

## Conventions

- **Strict mypy, no exceptions.** Production code is fully typed. `tests.*` is
  exempted from *requiring* annotations (see `[[tool.mypy.overrides]]`) but its
  bodies are still type-checked — don't add `# type: ignore` to dodge a real error.
- **Ruff** with `E, F, I, B, UP` selected, line length 88. Imports are sorted by
  ruff (`I`); `UP` keeps syntax modern.
- **`from __future__ import annotations`** at the top of every module.
- **Ports are `Protocol`s** (`@runtime_checkable`), not ABCs. Adapters satisfy
  them structurally — no explicit subclassing.
- **100% line + branch coverage** is required (`fail_under = 100`, `branch = true`).
  New code needs tests that exercise every branch. Tests use fakes for the ports
  and `respx` to stub slskd's HTTP API; mirror the existing patterns in `tests/`.
- **Error envelopes return HTTP 200.** Both Lidarr-facing surfaces expect a 200
  with an error *payload* (Newznab XML `<error>` / SABnzbd `{"status": false}`),
  not an HTTP error status. See the error handler in `adapters/inbound/app.py`.

## Gotchas

- **Multi-disc albums are unsupported** and this is by design, not a bug — slskd
  flattens each disc into its own folder with no shared album root, so the bridge
  cannot reassemble them. The blocked prerequisite and planned fix live in the
  `compute_storage_path` docstring in `domain/paths.py`. Don't "fix" this without
  reading that first.
- **Env vars are namespaced** (`SLSKD_*`, `BRIDGE_*`) and documented in the README
  table. Add new ones to both `config.py` and that table.

## Workflow

- **Work on a feature branch, never directly on `main`.** Use a git worktree
  when isolation helps (parallel work, keeping `main` clean for other tasks).
- **Commit freely while on a branch** — small, focused commits are encouraged and
  cost nothing here. Keep each commit passing the full gate (`ruff`, `mypy`,
  `pytest`) so the history stays bisectable.
- **Integrate via pull request.** Open a PR against `main` rather than pushing to
  it directly.
- Design notes and the original build plan are under `docs/specs/` and
  `docs/plans/`.
