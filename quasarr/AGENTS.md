# quasarr/ - Application Package

## Purpose

Main application code for Quasarr: the bridge that exposes itself to Radarr/Sonarr/Lidarr/Magazarr as a Newznab indexer plus SABnzbd client and autonomously drives JDownloader2.

## Ownership

- `quasarr/api/` - the HTTP surface (emulation endpoints, web UI, config API); first-run setup pages are served separately by `quasarr/storage/setup/`
- `quasarr/downloads/` - download orchestration, link classification, mirror selection, package state
- `quasarr/search/` - search/feed fan-out across source modules
- `quasarr/providers/` - shared services (state, JD client, logging, auth, sessions, notifications, metadata)
- `quasarr/storage/` - persistence (INI config, SQLite state) and first-run setup flows
- `quasarr/constants/` and `quasarr/__init__.py` - owned by this doc (no child AGENTS.md)

## Local Contracts

- `quasarr/__init__.py` `run()` is the application entrypoint: pins the multiprocessing start method to `spawn` on every platform (fork hands children objects bound to the parent process, and Python is moving away from it; the console-script entrypoint used by Docker enters here, so the pin must live in `run()` and not in `Quasarr.py`), builds the multiprocessing-Manager shared state, resolves the config path (`DOCKER` env → `/config`, otherwise the path stored in `./Quasarr.conf`), gates startup on `DataBase.maintain()` (integrity failure exits), runs the setup flows in order, spawns daemon processes (FlareSolverr checker, JDownloader reconnect loop, update checker), then blocks in `get_api()`.
- `quasarr/constants/__init__.py` is the single project-wide constants module: search/download categories, hoster and mirror alias tables, content regexes, `AUTO_DECRYPT_PATTERNS`/`PROTECTED_PATTERNS`, `PACKAGE_ID_PATTERN`, and request timeouts. `apply_timeout_slow_mode_settings()` hot-patches `*_REQUEST_TIMEOUT_SECONDS` globals in every already-imported `quasarr.*` module - renaming those globals anywhere silently breaks timeout slow mode.
- `shared_state` (in `quasarr/providers/`) is passed around as a module; `shared_state.values["config"]` and `["database"]` hold the `Config`/`DataBase` *classes*, called as `config("Section")` / `database("table")`.
- Two-letter source key convention: a source's lowercase key is simultaneously the module filename under `quasarr/search/sources/`, the `Hostnames` config key, and (uppercased) the log identifier. A same-key module under `quasarr/downloads/sources/` exists whenever release links need source-specific extraction - search-only sources (currently FX) have none, and their links flow through the download orchestrator's crypter-detection fallback. Hostname-specific logic belongs in those source modules and their helpers, never in shared glue code.

## Work Guidance

- Target Python 3.12+. snake_case for modules, functions, variables, and test methods; PascalCase for classes; 4-space indentation; keep modules focused on one responsibility.
- New modules start with `# -*- coding: utf-8 -*-` and the project header comment (`# Quasarr`, `# Project by https://github.com/rix1337`), matching the prevailing convention; a few helpers and `__init__.py` files omit it - do not retrofit headers.
- Place shared helpers in the existing `helpers/` packages instead of duplicating logic across sources or providers.
- Logging: import `trace/debug/info/warn/error` from `quasarr.providers.log`; messages may embed loguru color tags like `<g>...</g>`. New modules wanting a context emoji add an entry to `log._context_replace`.
- HTTP timeouts always come from `quasarr.constants` (`SEARCH_/FEED_/DOWNLOAD_/SESSION_REQUEST_TIMEOUT_SECONDS`); the User-Agent comes from `shared_state.values["user_agent"]`.
- Keep changes small, readable, and consistent with surrounding code; do not introduce a separate style in one file.

## Verification

- Lint: `uv run ruff check .` (Ruff also enforces import sorting; `quasarr` is configured as first-party in `pyproject.toml`)
- Full unit suite: `uv run python -X utf8 -m unittest discover -s tests`
- Before a PR: `uv run python -X utf8 pre-commit.py`

## Child DOX Index

- `quasarr/api/AGENTS.md` - HTTP surface: route registration, auth enforcement, SABnzbd/Newznab emulation contracts, CAPTCHA UI flows
- `quasarr/downloads/AGENTS.md` - download orchestration, link classification, package IDs, mirror-selection policy; indexes `sources/` and `linkcrypters/`
- `quasarr/search/AGENTS.md` - search orchestrator: fan-out, caching, gating, pagination; indexes `sources/`
- `quasarr/providers/AGENTS.md` - shared services catalog and contracts; indexes `sessions/` and `notifications/`
- `quasarr/storage/AGENTS.md` - config/DB persistence, locking invariants, categories; indexes `setup/`
