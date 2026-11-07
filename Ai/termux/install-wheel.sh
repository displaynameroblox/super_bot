#!/usr/bin/env bash
set -euo pipefail

if (( $# < 1 || $# > 2 )) || [[ ! -f ${1:-} || ${1:-} != *.whl ]]; then
    printf 'Usage: bash install-wheel.sh SUPER_BOT.whl [ANDROID_WHEEL_DIRECTORY]\n' >&2
    exit 2
fi

wheel=$1
pip_options=()
if (( $# == 2 )); then
    if [[ ! -d $2 ]]; then
        printf 'Wheel directory does not exist: %s\n' "$2" >&2
        exit 2
    fi
    pip_options+=(--find-links "$2")
fi

pkg install -y python python-pip python-cryptography
venv="$HOME/.local/share/super-bot/termux-venv"
python -m venv --system-site-packages "$venv"
if ! "$venv/bin/python" -m pip install --only-binary=:all: --timeout 15 --retries 1 "${pip_options[@]}" "$wheel"; then
    printf 'Install failed. If pip reports no matching distribution, supply a wheel directory built for this Termux Python and Android architecture. Source builds are disabled.\n' >&2
    exit 1
fi
"$venv/bin/python" -m pip check
"$venv/bin/python" -c 'import Ai.server'
printf 'Installed. From your configured project directory, run:\n  "%s/bin/super-bot-server"\n' "$venv"
