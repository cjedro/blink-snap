# Design: OAuth v2 mid-session refresh + JSON logging

Date: 2026-08-03  
Status: approved for implementation planning (amended)  
Scope: `snap.py`, `docker-entrypoint.sh` (optional); README note for `LOG_LEVEL`  
Out of scope: rewriting Blink OAuth, forking blinkpy, `serve.py` log overhaul

## Constraint

**Prefer blinkpy APIs everywhere.** Do not reimplement OAuth token HTTP calls, login flows, or session serialization. Only monkey-patch the one blinkpy method that is wrong for OAuth v2 mid-session refresh (`Auth.refresh_tokens`), by **delegating to existing blinkpy helpers** (`api.oauth_refresh_token`, `Auth._process_token_data`, `Auth.startup`). Persist sessions with `blink.save()`.

## Problem

After the container runs for some time (typically after the ~4 hour access-token lifetime), captures fail repeatedly with:

```text
ERROR: Login endpoint failed. Try again later.
ERROR: Capture #N failed:
```

Restarting the container restores captures until the next expiry window.

## Root cause

Blink tokens are obtained via **OAuth v2** (`client_id=ios`, `hardware_id`).

| Path | When | Mechanism |
|------|------|-----------|
| Startup | `Auth.startup()` | `api.oauth_refresh_token()` with iOS client + `hardware_id` |
| Mid-session | `Auth.query()` → `need_refresh()` → `Auth.refresh_tokens()` | Legacy `request_login(..., is_refresh=True)` with `client_id=android`, no real `hardware_id` |

Access tokens expire in ~4 hours (`expires_in=14400`). Mid-session refresh uses the legacy Android path against an iOS-issued refresh token → `LoginError` → blinkpy logs `Login endpoint failed. Try again later.` → `TokenRefreshFailed`.

The watch loop catches the exception and retries; every later capture hits the same broken refresh. Restart works because `startup()` uses OAuth v2 refresh.

## Goals

1. Fix mid-session token refresh so watch mode survives access-token expiry without a container restart.
2. Emit structured JSON logs verbose enough to diagnose auth/capture failures.
3. Soft-reconnect in the watch loop if refresh still fails (still via blinkpy `start` / `startup`).

## Non-goals

- Rewriting `serve.py` HTTP logging
- Custom Blink HTTP client or copy-pasted OAuth token requests
- Forking / vendoring blinkpy
- Proactive Docker restart as the primary fix
- Logging passwords, access tokens, or refresh tokens

## Approach

**Option A (chosen):** Monkey-patch `Auth.refresh_tokens` so it calls blinkpy’s existing OAuth v2 refresh path (same idea as the existing `_patch_blinkpy_oauth_signin`), plus watch-loop reconnect using blinkpy connect/start, plus JSON logging in our process.

### Alternatives considered

| Option | Summary | Why not |
|--------|---------|---------|
| Soft reconnect only | On auth error, `connect_blink` again | Still fails every ~4h until reconnect; leaves broken refresh in place |
| Replace / reimplement Blink auth | Own token HTTP + login | Violates “use blinkpy” constraint; out of scope |

## Design

### 1. Monkey-patch `Auth.refresh_tokens` (thin wrapper over blinkpy)

Replace `Auth.refresh_tokens` at import time (alongside the existing sign-in patch). Implementation must **only** call blinkpy:

1. If `refresh_token` and `hardware_id` are present, call `api.oauth_refresh_token(auth, refresh_token, hardware_id)`.
2. On truthy token data, call `await auth._process_token_data(token_data)`, clear `auth.is_errored`, return `True`.
3. Else fall back to `await auth.startup()` (blinkpy’s own startup already tries OAuth v2 refresh, then full OAuth login).
4. On success after fallback, clear `is_errored` and return `True`.
5. Catch `BlinkTwoFARequiredError` from `startup()`: do **not** prompt; log and raise `TokenRefreshFailed` (headless-safe).
6. On any other failure, raise `TokenRefreshFailed` with cause chained.

Do **not** reimplement the token POST, headers, or client_id selection — that stays inside blinkpy’s `oauth_refresh_token` / `startup`.

**HTTP status logging caveat:** `api.oauth_refresh_token` returns `None` on non-200 and does not expose status/body. Accept that limitation: log `refresh_path`, presence of tokens/`hardware_id`, exception type/message, and whether fallback ran. Do **not** duplicate the HTTP call just to capture status.

Keep the existing `oauth_signin` 202/412 patch unchanged.

### 2. Persist session immediately after refresh (via blinkpy)

Refresh tokens may rotate. Saving only after a successful capture is unsafe.

- After `connect_blink` / successful start, set `blink.auth.callback` to schedule persistence of `blink.auth.login_attributes` through blinkpy’s `blink.save(session_path)` (async: `asyncio.create_task` / equivalent from the sync callback).
- Also `await blink.save(...)` after successful capture and after successful watch-loop reconnect (same as today, plus reconnect).
- Do **not** hand-roll JSON session writes; use `blink.save`.

### 3. Watch-loop recovery (blinkpy reconnect, no process exit)

Refactor helpers so recovery cannot `sys.exit`:

- Split **fatal CLI exit** from **reconnectable errors**: `connect_blink` (or a sibling used by watch mode) must raise/return failure instead of `sys.exit` when called from the watch loop. One-shot CLI can still exit non-zero at `main` / `run`.
- Same for camera resolve when used from reconnect: return/raise, don’t `sys.exit` inside the loop.
- On `TokenRefreshFailed`, `LoginError`, `UnauthorizedError` (and `BlinkTwoFARequiredError` if it escapes):
  - Log `reconnect` with capture # and `error_type`.
  - Rebuild auth via blinkpy: reload session with existing `load_login_data`, `Auth(...)`, `await blink.start()` (and 2FA only if interactive CLI — in Docker/watch, treat 2FA as reconnect failure).
  - Re-resolve camera; on success continue; on failure log `reconnect_fail` and sleep until next interval (**do not exit** the process).
- Other capture errors: log type + message (traceback at DEBUG) and continue.

### 4. JSON logging

- JSON formatter → one object per line on stderr.
- Core: `ts` (ISO-8601 UTC), `level`, `logger`, `msg`.
- `event` values: `startup`, `capture_ok`, `capture_fail`, `token_refresh`, `token_refresh_fail`, `reconnect`, `reconnect_fail`.
- Context when relevant: `capture`, `camera`, `mode`, `path`, `error_type`, `refresh_path` (`oauth_v2` / `startup_fallback`), `expires_in_s`, `has_refresh_token`, `has_hardware_id`.
- Omit `http_status` unless blinkpy surfaces it without a custom HTTP client (see caveat above).
- Operational watch/auth messages via `logging`; keep interactive 2FA prompts as human text.
- Default level **INFO** (behavior change from today’s WARNING — intentional for Docker).
- Overrides: `-v` / `--verbose` → DEBUG; `LOG_LEVEL` env read in `snap.py` (`DEBUG|INFO|WARNING|ERROR`).
- blinkpy loggers: INFO normally; DEBUG when app DEBUG.

### 5. Docker / CLI

- `snap.py` reads `LOG_LEVEL` directly; entrypoint need not translate it (optional doc only).
- Document `LOG_LEVEL` in README.
- No volume/layout changes.

## Error handling

| Failure | Behavior |
|---------|----------|
| `oauth_refresh_token` returns `None` | Log `token_refresh_fail`; call blinkpy `startup()` fallback |
| `startup()` raises `BlinkTwoFARequiredError` | Log headless-safe message; raise `TokenRefreshFailed`; watch reconnect may fail until interactive host re-auth |
| Transient network / other capture errors | Log and retry next interval |
| Reconnect failure | Log `reconnect_fail`; do not exit; wait for interval |

## Testing

Repo currently has no test suite. Keep verification lean and blinkpy-oriented:

- Prefer a small scripted/manual check: force `auth.expiration_date` into the past after a real `connect_blink`, trigger one capture/refresh, confirm success and session file update via `blink.save`.
- Optional later: pytest with mocks of `api.oauth_refresh_token` only if we add a test harness; not required for this change.
- Regression: `--list`, one-shot capture, interactive 2FA path unchanged.

## Rollout

1. Implement + manual verify locally / Docker.
2. Publish image per existing workflow; redeploy compose.
3. Monitor JSON logs around the ~4h mark.

## Success criteria

- Watch mode keeps capturing after access-token expiry without container restart.
- Auth/capture failures emit JSON with `event` + `error_type` (and refresh context fields above).
- No custom Blink OAuth HTTP implementation; only the thin `refresh_tokens` monkey-patch plus our logging/reconnect orchestration.
- Restart is no longer required for the known mid-session refresh failure mode.

## Amendments (vs first draft)

1. Reconnect must not reuse `sys.exit`-ing helpers as-is.
2. Persist session immediately after refresh via blinkpy `save` / `callback` (token rotation).
3. Headless-safe handling of `BlinkTwoFARequiredError` (no `input()` in Docker watch).
4. Do not reimplement token HTTP for status codes; use blinkpy and log what it exposes.
5. Soften testing to manual/blinkpy-driven verification unless a harness is added.
6. Explicit constraint: use blinkpy wherever possible; patch only `refresh_tokens`.
