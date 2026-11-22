"""Managed server child; holds the instance lock for its entire lifetime."""
import contextlib
import os
import subprocess
from pathlib import Path
import sys
import threading
import time
from Ai.managed_model import local_model
from Ai.manager_runtime import FileLock, atomic_json, read_json


def write_telemetry(root, state):
    """A full disk must not terminate the thread that handles shutdown."""
    try:
        atomic_json(root / 'telemetry.json', state)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def managed_discord(root, source, startup):
    configured = os.environ.get("SUPER_BOT_DISCORD_ENV", "").strip()
    if not configured:
        yield lambda: "disabled"
        return

    import certifi

    ready = root / f"discord-ready-{startup}"
    env = {**os.environ, "SUPER_BOT_DISCORD_READY": str(ready)}
    env.setdefault("SSL_CERT_FILE", certifi.where())
    command = [
        sys.executable, "-E", "-B", "-u", "-c",
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from Ai.discord_app import managed_main; raise SystemExit(managed_main(sys.argv[1]))",
        str(source), str(Path(configured).expanduser().resolve()),
    ]
    log_path = root / "discord.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        with os.fdopen(fd, "ab") as log:
            bot = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env)
    except OSError:
        print("Discord child could not be started.", flush=True)
        yield lambda: "failed"
        return

    def state():
        if bot.poll() is not None:
            return "failed"
        return "ready" if ready.is_file() else "connecting"

    try:
        yield state
    finally:
        bot.stdin.close()
        try:
            bot.wait(timeout=3)
        except subprocess.TimeoutExpired:
            bot.kill()
            bot.wait()
        ready.unlink(missing_ok=True)


def main():
    root, source, build, startup = sys.argv[1:]
    root = Path(root)
    with FileLock(root / 'server.lock'), local_model(root, startup) as model:
        import Ai
        Ai.__path__ = [str(Path(source) / 'Ai')]
        from Ai import server
        import uvicorn
        original_run = uvicorn.run

        def managed_run(app, **options):
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            cert = x509.load_pem_x509_certificate(Path(options['ssl_certfile']).read_bytes())
            state = {'pid': os.getpid(), 'build': build, 'startup_id': startup,
                     'port': options['port'], 'host': options['host'],
                     'probe_host': {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(options['host'], options['host']),
                     'tls_fingerprint': cert.fingerprint(hashes.SHA256()).hex()}
            atomic_json(root / 'process.json', state)
            runner = uvicorn.Server(uvicorn.Config(app, **options, timeout_graceful_shutdown=15))
            done = threading.Event()

            def control(discord_state):
                while not done.is_set():
                    if model is not None and model.poll() is not None:
                        print('Local model exited. See model.log.', flush=True)
                        runner.should_exit = True
                    if read_json(root / 'stop.json', {}).get('startup_id') == startup:
                        runner.should_exit = True
                    write_telemetry(root, {
                        'startup_id': startup, 'sessions': len(server.chat_sessions),
                        'clients': sum(len(peers) for peers in tuple(server.event_manager.connections.values())),
                        'discord': discord_state(),
                        'minecraft_bridge': 'enabled' if os.getenv('SUPER_BOT_SBP_CONFIG', '').strip() else 'disabled'})
                    done.wait(.3)
            with managed_discord(root, source, startup) as discord_state:
                thread = threading.Thread(target=control, args=(discord_state,), daemon=True)
                thread.start()
                try:
                    runner.run()
                finally:
                    done.set()
                    thread.join(timeout=2)
        os.environ['SUPER_BOT_RELEASE_BUILD'] = build
        uvicorn.run = managed_run
        try:
            server.start_server()
        finally:
            uvicorn.run = original_run


if __name__ == '__main__':
    main()
