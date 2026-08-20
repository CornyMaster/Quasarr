# quasarr/providers/sessions/ - Per-Source Sessions

## Purpose

Authenticated `requests.Session` modules for login-required sources. Every new login-required source replicates this contract exactly.

## Ownership

- `al.py` - FlareSolverr-backed session with JSON-envelope expiry; `fetch_via_flaresolverr` / `fetch_via_requests_session` helpers, both re-persisting cookies after each call
- `dd.py` - CSRF-token + JSON login (`/api/csrf-token` then `/api/auth/login`); blank stored credentials on a rejected login. `nx.py` - simple login session; blank stored credentials on a rejected login
- `dl.py` - CSRF login flow; its fetch helper re-persists cookies after each call

## Local Contracts

- Module shape: module-level `hostname = "<xx>"` shorthand; `create_and_persist_session(shared_state)` and `retrieve_and_validate_session(shared_state)` returning `requests.Session` or `None` (creating + persisting on miss).
- Sessions persist as `base64(pickle(Session))` in DB table `sessions`, keyed by shorthand. AL wraps the pickle in a JSON envelope `{token, created_at}` with expiry - changing any serialization format requires that style of invalidate-and-recreate migration.
- Guard with `utils.is_site_usable`; call `mark_hostname_issue(hostname, "session", msg)` on failure and `clear_hostname_issue(hostname)` on success.

## Work Guidance

- Timeouts from `constants.SESSION_REQUEST_TIMEOUT_SECONDS`; never hardcode source hostnames.

## Verification

- Targeted test: `test_al_flaresolverr_session.py`; full suite: `uv run python -X utf8 -m unittest discover -s tests`

## Child DOX Index

None.
