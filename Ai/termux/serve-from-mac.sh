#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
MAC_IP=""
if command -v ipconfig >/dev/null 2>&1; then
    if MAC_IP="$(ipconfig getifaddr en0)"; then
        :
    fi
fi

if [[ -n "$MAC_IP" ]]; then
    printf 'On the phone, download the script with:\n'
    printf '  curl --fail --location http://%s:8765/termux/start-server.sh -o start-server.sh\n' "$MAC_IP"
else
    printf 'Find the Mac Wi-Fi IP in System Settings > Network, then use:\n'
    printf '  curl --fail --location http://MAC_IP:8765/termux/start-server.sh -o start-server.sh\n'
fi
printf 'Serving the Ai folder on port 8765. Press Ctrl+C to stop.\n'

exec python3 -m http.server 8765 \
    --bind 0.0.0.0 \
    --directory "$SCRIPT_DIR"
