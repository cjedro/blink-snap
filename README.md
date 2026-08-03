# Blink Mini — Local Snapshot CLI

Small CLI that captures still images from Blink cameras (including the **Blink Mini**) and saves them under `captures/`.

Uses [blinkpy](https://github.com/fronzbot/blinkpy) on top of the community-documented [BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol) API.

## Setup

```bash
cd blink
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add BLINK_USERNAME and BLINK_PASSWORD
```

`live` and `clip` modes need **ffmpeg** installed (`brew install ffmpeg` on macOS).

## Usage

```bash
# List cameras (use the exact name from the Blink app)
python snap.py --list

# Fast snapshot — thumbnail API (low resolution)
python snap.py --camera "Living Room"

# Higher resolution — grab one frame from live stream (~1080p)
python snap.py --camera "Living Room" --mode live

# Higher resolution — record a short clip, extract first frame (~1080p)
python snap.py --camera "Living Room" --mode clip --keep-video

# Custom output directory
python snap.py --camera "Living Room" --output ./captures

# Capture every 5 minutes until Ctrl+C (minimum interval: 60s)
python snap.py --camera "Living Room" --interval 300
```

### Watch mode (`--interval`)

Runs forever, taking one picture every `SECONDS` (minimum **60** — Blink asks clients not to poll faster than once per minute). Uses thumbnail mode only. Mid-session token refresh uses Blink OAuth v2 (via blinkpy); the session file is updated after refresh and after each successful capture. On auth failure the watch loop reconnects without exiting. Press **Ctrl+C** to stop.

Operational logs are JSON lines on stderr. Default level is `INFO`. Set `LOG_LEVEL=DEBUG` (or pass `-v`) for more detail.

CLI args can be replaced with env vars: `CAMERA` / `BLINK_CAMERA`, `INTERVAL` / `BLINK_INTERVAL`, `BLINK_OUTPUT`, `BLINK_SESSION`, `LOG_LEVEL`.

## Docker

Runs the interval watch loop in a container. Credentials and settings come from environment variables; mount `blink_session.json` and `captures/` from the host.

### Prerequisites

1. Log in once on the host so `blink_session.json` exists (needs interactive 2FA):

   ```bash
   python snap.py --list
   ```

2. Copy the Docker env template:

   ```bash
   cp .env.docker.example .env.docker
   # edit BLINK_USERNAME, BLINK_PASSWORD, CAMERA, INTERVAL
   ```

### Run with Docker Compose

```bash
docker compose --env-file .env.docker up -d
```

Uses the published image `ghcr.io/cjedro/blink-snap:latest` (pulls on start).

Volumes (in `docker-compose.yml`):

| Host | Container | Purpose |
|------|-----------|---------|
| `./blink_session.json` | `/data/blink_session.json` | Auth tokens (refresh in-container) |
| `./captures` | `/data/captures` | Saved JPEGs |

### Run with plain Docker

```bash
docker run -d --name blink-snap \
  -e BLINK_USERNAME='your@email.com' \
  -e BLINK_PASSWORD='your-password' \
  -e CAMERA='Drucki' \
  -e INTERVAL=300 \
  -v "$(pwd)/blink_session.json:/data/blink_session.json" \
  -v "$(pwd)/captures:/data/captures" \
  --restart unless-stopped \
  ghcr.io/cjedro/blink-snap:latest
```

The container has no TTY — **create `blink_session.json` on the host first** (2FA step). After that, the container refreshes tokens and writes the updated session back to the mounted file.

### First run

1. Blink uses OAuth + two-step verification (email or SMS). On first login you will be prompted for the code.
2. Tokens are saved to `blink_session.json` so later runs skip 2FA until the session expires.

## Capture modes and resolution

| Mode | API | Typical resolution | Speed | Notes |
|------|-----|-------------------|-------|-------|
| `thumbnail` (default) | `snap_picture()` + thumbnail URL | **1920×1080** on Blink Mini (verified) | Fast (~5–15s) | Recommended — same path as Blink “Refresh Thumbnail” |
| `live` | liveview + `immis://` proxy + ffmpeg | Up to 1080p | Medium | Often blocked with `An app update is required` from Blink’s API |
| `clip` | `record()` + MP4 + ffmpeg | Up to 1080p | Slow | Clip download may fail without subscription |

On a Blink Mini, **`--mode thumbnail` already returns full 1080p JPEGs** in practice. `live` and `clip` are experimental fallbacks; Blink currently rejects liveview for unofficial clients.

After each capture the CLI prints file size and measured JPEG dimensions.

## Project layout

```text
blink/
├── snap.py              # CLI entry point
├── requirements.txt
├── .env.example
├── captures/            # saved images (gitignored)
├── blink_session.json   # cached auth (gitignored)
├── PLAN.md              # design notes and trade-offs
└── README.md
```

## References

- [BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol)
- [blinkpy](https://github.com/fronzbot/blinkpy)
- [PLAN.md](./PLAN.md) — architecture, pros/cons, risks
