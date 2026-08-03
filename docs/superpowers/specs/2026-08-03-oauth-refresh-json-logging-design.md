# Design: OAuth v2 mid-session refresh + JSON logging

Date: 2026-08-03  
Status: approved for implementation planning  
Scope: `snap.py`, `docker-entrypoint.sh` (optional env passthrough); not `serve.py` rewrite

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

Access tokens expire in ~4 hours (`expires_in=14400`). When refresh is needed mid-watch, blinkpy uses the legacy Android refresh against an iOS-issued refresh token. That fails with a generic `LoginError`, which blinkpy logs as `Login endpoint failed. Try again later.` and raises `TokenRefreshFailed`.

The watch loop catches the exception, sleeps, and retries. Every later capture hits `need_refresh()` again and fails the same way. Restart works because `startup()` uses the correct OAuth v2 refresh.

## Goals

1. Fix mid-session token refresh so long-running watch mode survives access-token expiry without a container restart.
2. Emit structured JSON logs (verbose enough to diagnose auth/capture failures next time).
3. Soft-reconnect in the watch loop if refresh still fails.

## Non-goals

- Rewriting `serve.py` access/export HTTP logging
- Replacing blinkpy with a custom Blink client
- Proactive Docker restart / healthcheck-based bounce as the primary fix
- Logging passwords, access tokens, or refresh tokens

## Approach

**Option A (chosen):** Monkey-patch blinkpy’s mid-session refresh to use OAuth v2 (same pattern as the existing `_patch_blinkpy_oauth_signin`), plus watch-loop reconnect and JSON logging.

### Alternatives considered

| Option | Summary | Why not |
|--------|---------|---------|
| Soft reconnect only | On auth error, call `connect_blink` again | Still fails every ~4h until reconnect; does not fix the bad refresh path |
| Replace blinkpy auth | Own full OAuth client | Out of scope for this incident |

## Design

### 1. Monkey-patch `Auth.refresh_tokens`

Extend the blinkpy patching already done in `snap.py`:

1. Prefer `api.oauth_refresh_token(auth, refresh_token, hardware_id)`.
2. On success, call `auth._process_token_data(token_data)` (or equivalent field updates already used at startup).
3. If OAuth v2 refresh returns no token data or errors, fall back to `await auth.startup()` (full OAuth v2 login/refresh path used at process start).
4. If both fail, raise `TokenRefreshFailed` with a useful chained cause.
5. Log structured events: which path was used (`oauth_v2` vs `startup_fallback`), HTTP status / short body snippet when available, seconds until prior expiry — never secrets.

Keep the existing `oauth_signin` 202/412 patch unchanged.

### 2. Watch-loop recovery

In `watch_loop`:

- On `TokenRefreshFailed`, `LoginError`, or related unauthorized/auth failures:
  - Log `reconnect` attempt with capture number and error type.
  - Re-run connect/login via existing `connect_blink` (or equivalent) using the same `ClientSession` / session path.
  - Re-resolve the camera by name.
  - Continue the loop on success; on failure log `reconnect_fail` and wait for the next interval (do not exit the process).
- On other capture errors: log full exception type + message (+ traceback at DEBUG).
- Save session after successful capture, successful mid-session refresh (if we own that save point), and successful reconnect.

### 3. JSON logging

- Configure `logging` with a JSON formatter writing one object per line to stderr.
- Core fields: `ts` (ISO-8601 UTC), `level`, `logger`, `msg`.
- Event field `event` for machine parsing, including at least:
  - `startup`
  - `capture_ok`
  - `capture_fail`
  - `token_refresh`
  - `token_refresh_fail`
  - `reconnect`
  - `reconnect_fail`
- Contextual fields when relevant: `capture`, `camera`, `mode`, `path`, `error_type`, `http_status`, `refresh_path`, `expires_in_s`.
- Move operational watch/auth messages from bare `print` to the logger where practical; keep interactive CLI prompts (2FA input) as human text on stderr/stdout.
- Default level: **INFO** (especially in Docker).
- Overrides: `--verbose` / `-v` → DEBUG; env `LOG_LEVEL` (`DEBUG|INFO|WARNING|ERROR`).
- blinkpy loggers: INFO by default; DEBUG when app is DEBUG so library refresh details are visible without always flooding.

### 4. Docker / CLI wiring

- `docker-entrypoint.sh`: honor optional `LOG_LEVEL` (document in README).
- No change required to compose volume layout.
- Existing `-v` continues to work for local CLI runs.

## Error handling

| Failure | Behavior |
|---------|----------|
| OAuth v2 refresh HTTP non-200 | Log status + short body; try `startup()` fallback |
| `startup()` needs 2FA in container | Log clearly; reconnect fails; process keeps interval (operator must re-auth interactively on host) |
| Transient network error | Log and retry next interval |
| Non-auth capture error | Log and continue |

## Testing

- Unit-style: patch/mock `oauth_refresh_token` success and failure paths; assert refresh path selection and that `TokenRefreshFailed` is raised only after both paths fail.
- Manual / Docker: run with short forced expiry simulation (mock `expiration_date` in past) and confirm refresh succeeds without restart; confirm JSON log lines on success/failure.
- Regression: `--list` and one-shot capture still work; 2FA interactive path unchanged.

## Rollout

1. Implement + test locally.
2. Bump image / tag as usual for this repo’s publish workflow.
3. Redeploy compose service; monitor JSON logs around the 4h mark.

## Success criteria

- Watch mode continues capturing after access-token expiry without container restart.
- Failed auth/capture emits JSON logs with `event`, `error_type`, and enough HTTP/context fields to diagnose without secrets.
- Restart is no longer required for the known mid-session refresh failure mode.
