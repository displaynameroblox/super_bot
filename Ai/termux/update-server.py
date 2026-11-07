#!/usr/bin/env python3
"""Stage and supervise a signed Super Bot release independently of its code."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

MAX_RELEASE_BYTES = 64 * 1024 * 1024
BUILD_ID = re.compile(r"[0-9a-f]{64}")
RELEASE_NAME = "super-bot-release.tar.gz"


def download(url, path, limit):
    deadline = time.monotonic() + 120
    with urllib.request.urlopen(url, timeout=10) as response, path.open("xb") as output:
        while chunk := response.read1(65536):
            limit -= len(chunk)
            if limit < 0 or time.monotonic() >= deadline:
                raise ValueError("Release download exceeded its size or time limit")
            output.write(chunk)


def stage(root, url, key):
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as directory:
        work = Path(directory)
        archive = work / RELEASE_NAME
        signature = work / "signature"
        download(f"{url.rstrip('/')}/{RELEASE_NAME}.sig", signature, 16384)
        download(f"{url.rstrip('/')}/{RELEASE_NAME}", archive, MAX_RELEASE_BYTES)
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(key), "-signature",
             str(signature), str(archive)],
            check=True, timeout=15, stdout=subprocess.DEVNULL,
        )
        build = hashlib.sha256(archive.read_bytes()).hexdigest()
        release = root / build
        if release.is_dir():
            return build

        tree = work / "tree"
        tree.mkdir()
        remaining = MAX_RELEASE_BYTES
        with tarfile.open(archive, "r:gz") as bundle:
            for index, member in enumerate(bundle):
                path = PurePosixPath(member.name)
                if (index >= 2048 or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                        or path.is_absolute() or "\\" in member.name or ":" in member.name
                        or len(path.parts) < 2 or path.parts[0] != "Ai"
                        or any(part.startswith(".") for part in path.parts) or member.size < 0):
                    raise ValueError(f"Invalid release member: {member.name!r}")
                remaining -= member.size
                if remaining < 0:
                    raise ValueError("Expanded release exceeds its size limit")
                destination = tree.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
        for filename in ("__init__.py", "server.py", "gateway.py", "requirements.txt"):
            if not (tree / "Ai" / filename).is_file():
                raise ValueError(f"Release is missing Ai/{filename}")
        (tree / "Ai" / ".release-build").write_text(build, encoding="ascii")
        tree.rename(release)
        return build


def active_build(root):
    pointer = root / "active"
    if not pointer.exists():
        return None
    build = pointer.read_text(encoding="ascii").strip()
    if not BUILD_ID.fullmatch(build) or not (root / build / "Ai" / "server.py").is_file():
        raise ValueError("Invalid active release pointer")
    return build


def commit(root, build):
    fd, name = tempfile.mkstemp(dir=root)
    pending = Path(name)
    try:
        with os.fdopen(fd, "w") as pointer:
            pointer.write(build + "\n")
            pointer.flush()
            os.fsync(pointer.fileno())
        os.replace(pending, root / "active")
    finally:
        pending.unlink(missing_ok=True)


def healthy(port, build, startup_id):
    # Loopback uses the server's existing self-signed certificate. The per-launch
    # ID distinguishes this child from an old process serving the same build.
    connection = http.client.HTTPSConnection(
        "127.0.0.1", port, timeout=1, context=ssl._create_unverified_context()
    )
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return False
        payload = json.loads(response.read(4096))
        return payload == {"status": "ok", "build": build, "startup_id": startup_id}
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        connection.close()


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def launch(install, source, port):
    startup_id = secrets.token_hex(16)
    command = (
        "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
        "runpy.run_module('Ai.server', run_name='__main__')"
    )
    process = subprocess.Popen(
        [sys.executable, "-E", "-B", "-c", command, str(source)],
        cwd=install,
        env={**os.environ, "PORT": str(port), "SUPER_BOT_STARTUP_ID": startup_id},
        start_new_session=True,
    )
    return process, startup_id


def verify_startup(process, port, build, startup_id, timeout):
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        if healthy(port, build, startup_id) and process.poll() is None:
            return
        time.sleep(0.2)
    raise RuntimeError(f"Build {build} did not pass startup verification")


def run(install, url, key, port, timeout):
    root = install / ".releases"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write("0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = active_build(root)
        candidate = previous
        failed = False
        if url:
            try:
                candidate = stage(root, url, key)
            except (OSError, ValueError, http.client.HTTPException, tarfile.TarError,
                    subprocess.SubprocessError) as exc:
                print(f"Update rejected: {exc}", file=sys.stderr, flush=True)
                failed = True

        process = None
        try:
            if candidate is not None and candidate != previous:
                try:
                    process, startup_id = launch(install, root / candidate, port)
                    verify_startup(process, port, candidate, startup_id, timeout)
                    commit(root, candidate)
                except (OSError, RuntimeError) as exc:
                    if process is not None:
                        stop(process)
                        process = None
                    print(f"Update failed: {exc}. Starting the previous release.", file=sys.stderr, flush=True)
                    failed = True

                else:
                    print(f"Update verified and activated: {candidate}", flush=True)
                    return process.wait()

            source = root / previous if previous else install
            if not (source / "Ai" / "server.py").is_file():
                raise RuntimeError("No previous server installation is available")
            process, startup_id = launch(install, source, port)
            if previous:
                verify_startup(process, port, previous, startup_id, timeout)
                if failed:
                    print(f"Previous release verified: {previous}", flush=True)
            result = process.wait()
            return result or int(failed)
        finally:
            if process is not None:
                stop(process)


def configured_port(install):
    config = install / ".env"
    values = dict(os.environ)
    if config.is_file() and "SUPER_BOT_PORT" not in values:
        from dotenv import dotenv_values

        values = {**dotenv_values(config), **values}
    return int(values.get("SUPER_BOT_PORT") or values.get("PORT") or values.get("port") or "8000")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, required=True)
    parser.add_argument("--url")
    parser.add_argument("--key", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--startup-timeout", type=float, default=90)
    args = parser.parse_args()
    if args.url and not args.key:
        parser.error("--url requires a trusted --key")
    if not 0 < args.startup_timeout <= 600:
        parser.error("Invalid startup timeout")

    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        install = args.install.resolve()
        port = args.port if args.port is not None else configured_port(install)
        if not 1 <= port <= 65535:
            raise ValueError("Server port must be between 1 and 65535")
        return run(install, args.url, args.key, port, args.startup_timeout)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Super Bot launcher: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
