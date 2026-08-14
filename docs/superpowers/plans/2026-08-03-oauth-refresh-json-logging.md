# OAuth v2 refresh + JSON logging Implementation Plan

> **For agentic workers:** Use this plan task-by-task. Steps use checkbox syntax.

**Goal:** Fix mid-session Blink token refresh via a thin blinkpy monkey-patch, add watch-loop reconnect, and switch operational logs to JSON.

**Architecture:** Patch `Auth.refresh_tokens` to call `api.oauth_refresh_token` / `Auth.startup`; persist with `blink.save` via `auth.callback`; refactor connect/resolve so watch mode never `sys.exit`s on recoverable auth errors; JSON logging in `snap.py`.

**Tech Stack:** Python 3.12, blinkpy 0.25.5, aiohttp, stdlib `logging`

## Global Constraints

- Use blinkpy APIs wherever possible; do not reimplement OAuth HTTP.
- Never log passwords or tokens.
- Headless-safe: no `input()` on 2FA during watch/Docker refresh paths.
- Spec: `docs/superpowers/specs/2026-08-03-oauth-refresh-json-logging-design.md`

---

### Task 1: JSON logging + log level config in `snap.py`

- [ ] Add `JsonFormatter` and `configure_logging(level)`
- [ ] Resolve level from `-v` / `LOG_LEVEL` (default INFO)
- [ ] Wire in `main()`; set blinkpy loggers appropriately

### Task 2: Patch `Auth.refresh_tokens`

- [ ] Add `_patch_blinkpy_refresh_tokens()` using only blinkpy helpers
- [ ] Log `token_refresh` / `token_refresh_fail` with `refresh_path`, `expires_in_s`, flags
- [ ] On `BlinkTwoFARequiredError` from startup fallback: raise `TokenRefreshFailed` (no prompt)

### Task 3: Session save callback + reconnectable connect/resolve

- [ ] `attach_session_saver(blink, session_path)` via `auth.callback` → `asyncio.create_task(blink.save(...))`
- [ ] Refactor `connect_blink` to raise on failure; CLI `run`/`main` exits on failure for one-shot
- [ ] Refactor `resolve_camera` to raise `ValueError` (or custom) instead of `sys.exit` when `exit_on_error=False`

### Task 4: Watch-loop recovery + README

- [ ] On auth errors: reconnect with blinkpy start, re-resolve camera, continue
- [ ] Structured capture_ok / capture_fail / reconnect events
- [ ] Document `LOG_LEVEL` in README
- [ ] Syntax-check / import-check with venv

### Task 5: Verify

- [ ] `python -m py_compile snap.py`
- [ ] Manual reasoning check against design success criteria
