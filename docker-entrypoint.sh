#!/bin/sh
set -eu

missing=""
for var in BLINK_USERNAME BLINK_PASSWORD CAMERA INTERVAL; do
    eval "value=\${$var:-}"
    if [ -z "$value" ]; then
        missing="$missing $var"
    fi
done

if [ -n "$missing" ]; then
    echo "Missing required environment variables:$missing" >&2
    exit 1
fi

exec python snap.py \
    --camera "$CAMERA" \
    --interval "$INTERVAL" \
    --session "${BLINK_SESSION:-/data/blink_session.json}" \
    --output "${BLINK_OUTPUT:-/data/captures}" \
    "$@"
