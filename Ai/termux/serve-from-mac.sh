#!/usr/bin/env bash
set -euo pipefail

if (($# > 1)); then
    printf 'Usage: %s [/path/to/release-private.pem]\n' "$0" >&2
    exit 2
fi
SIGNING_KEY="${1:-${XDG_CONFIG_HOME:-$HOME/.config}/superbot/release-private.pem}"
if [[ ! -f "$SIGNING_KEY" ]]; then
    if (($#)); then
        printf 'Signing key not found: %s\n' "$SIGNING_KEY" >&2
        exit 1
    fi
    mkdir -p "$(dirname -- "$SIGNING_KEY")"
    (umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$SIGNING_KEY")
fi
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLISH_DIR="$(mktemp -d)"
trap 'rm -r -- "$PUBLISH_DIR"' EXIT
archive="$PUBLISH_DIR/super-bot-release.tar.gz"
python3 - "$SOURCE_DIR" "$archive" <<'PY'
import gzip
from pathlib import Path
import sys
import tarfile

source, archive = map(Path, sys.argv[1:])
files = [*source.glob("*.py"), source / "requirements.txt"]
files.extend(path for path in (source / "web").rglob("*")
             if path.suffix in {".html", ".js", ".css"})
with archive.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as zipped:
    with tarfile.open(fileobj=zipped, mode="w") as bundle:
        for path in sorted(files):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Expected a regular source file: {path}")
            name = "Ai/" + path.relative_to(source).as_posix()
            info = tarfile.TarInfo(name)
            info.size = path.stat().st_size
            with path.open("rb") as content:
                bundle.addfile(info, content)
PY
openssl dgst -sha256 -sign "$SIGNING_KEY" -out "$archive.sig" "$archive"

cp "$SOURCE_DIR/termux/start-server.sh" "$PUBLISH_DIR/start-server.sh"
cp "$SOURCE_DIR/termux/update-server.py" "$PUBLISH_DIR/update-server.py"
cp "$SOURCE_DIR/requirements.txt" "$PUBLISH_DIR/requirements.txt"
openssl pkey -in "$SIGNING_KEY" -pubout -out "$PUBLISH_DIR/release-public.pem"
mkdir "$PUBLISH_DIR/termux"
cp "$PUBLISH_DIR/start-server.sh" "$PUBLISH_DIR/update-server.py" "$PUBLISH_DIR/termux/"
printf 'Bootstrap files: /start-server.sh, /update-server.py, /requirements.txt, /release-public.pem\n'
printf 'Signing key retained at %s. Transfer the public key over a trusted connection.\n' "$SIGNING_KEY"
printf 'After installation, phone menu option 6 runs independently; option 7 checks for updates.\n'
printf 'Serving the signed release on port 8765. Press Ctrl+C to stop.\n'
python3 -m http.server 8765 --bind 0.0.0.0 --directory "$PUBLISH_DIR"
