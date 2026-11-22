"""Exercise the actual Java HTTPS transport against a temporary Python SBP host."""
from pathlib import Path
import os
import json
import socket
import subprocess
import tempfile
import threading
import time
import uvicorn
from Ai import tls
from Ai import phone_model
from Ai.universal.host import Host
from Ai.universal.http import create_app
from Ai.universal.security import Principal, digest, new_token
from Ai.universal.runtime import ExistingProvider, Runtime
from Ai.universal.store import Store

with tempfile.TemporaryDirectory(prefix='minecraft-sbp-') as tmp:
    root = Path(tmp)
    credential = new_token()
    caps = frozenset({'minecraft.observe', 'minecraft.stop', 'minecraft.move_to'})
    host = Host(Store(root / 'host.db'), [Principal('sdk', digest(credential), 'adapter', frozenset({'world'}),
        caps, caps, models=frozenset({'existing'}))])
    runtime = Runtime(host)
    def fake_chat(messages):
        text = messages[-1]['content']
        if text == 'Remember Alex':
            return 'I will remember.'
        if text == 'What is my name?':
            return 'Alex' if any(row['content'] == 'Remember Alex' for row in messages) else 'Unknown'
        goal = json.loads(text)['goal']
        return '{"x":3,"y":64,"z":0}' if goal.startswith('You control a Minecraft NPC.') else 'Hello from the model'
    phone_model.chat = fake_chat
    runtime.register_model('existing', ExistingProvider())
    cert = tls.prepare_server_tls(directory=root / 'tls')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    app = create_app(host, runtime)
    with host.store.transaction():
        node_id = host.store.get('metadata', 'node')['node_id']
    server = uvicorn.Server(uvicorn.Config(app, log_level='error',
        ssl_certfile=str(cert.certfile), ssl_keyfile=str(cert.keyfile)))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        if not server.started: raise RuntimeError('Temporary host did not start')
        sdk = Path(__file__).resolve().parents[1]
        subprocess.run([os.environ.get('GRADLE_EXECUTABLE', str(sdk / 'gradlew')), ':common:integrationProbe', '--configure-on-demand',
            f'--args=https://127.0.0.1:{port} {cert.certfile} {node_id}', '--no-daemon'], cwd=sdk,
            env={**os.environ, 'SBP_ADAPTER_TOKEN': credential}, check=True)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        host.store.close()
