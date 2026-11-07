"""Managed server child; holds the instance lock for its entire lifetime."""
import os
from pathlib import Path
import sys
import threading
import time
from Ai.managed_model import local_model
from Ai.manager_runtime import FileLock, atomic_json, read_json


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

            def control():
                while not done.wait(.3):
                    if model is not None and model.poll() is not None:
                        print('Local model exited. See model.log.', flush=True)
                        runner.should_exit = True
                    if read_json(root / 'stop.json', {}).get('startup_id') == startup:
                        runner.should_exit = True
                    atomic_json(root / 'telemetry.json', {
                        'startup_id': startup, 'sessions': len(server.chat_sessions),
                        'clients': sum(len(peers) for peers in tuple(server.event_manager.connections.values()))})
            thread = threading.Thread(target=control, daemon=True)
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
