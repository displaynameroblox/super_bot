"""Run an optional local inference process for the lifetime of a managed server."""
import contextlib
import os
import sys
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

from Ai.manager_runtime import read_json


def runner_executable():
    executable = shutil.which('llama-server')
    if executable or sys.platform != 'darwin':
        return executable
    for directory in ('/opt/homebrew/bin', '/usr/local/bin'):
        executable = shutil.which('llama-server', path=directory)
        if executable:
            return executable
    return None


def selected_model_path(root):
    from dotenv import dotenv_values

    configuration = {
        **dotenv_values(Path.cwd() / '.env'),
        **dotenv_values(root / '.env'),
        **os.environ,
    }
    if str(configuration.get('PHONE_MODEL_PROVIDER', '')).strip().lower() in {'openai', 'gemini'}:
        return None
    selection = read_json(root / 'model.json')
    return Path(selection['path']) if selection else None


def check_model_ready(root):
    path = selected_model_path(root)
    if path is None:
        return
    if not path.is_file():
        raise RuntimeError(f'Selected model is missing: {path}. Download it again or pick another model in admin.')
    if not runner_executable():
        raise RuntimeError('llama-server is required for the selected GGUF. Open admin and choose Start to install it.')


@contextlib.contextmanager
def local_model(root, startup):
    path = selected_model_path(root)
    if path is None:
        yield None
        return

    check_model_ready(root)
    executable = runner_executable()

    import httpx

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}/v1'
    key = secrets.token_urlsafe(32)
    command = [  # FOR THE TESTERS, THIS MATTERS THE MOST!
    executable,
    '--model', str(path),
    '--host', '127.0.0.1',
    '--port', str(port),
    '--alias', 'superbot-phone',
    '--api-key', key,

    # Context and concurrency
    '--ctx-size', '8192',
    '--parallel', '1',

    # CPU and GPU
    '--threads', '4',
    '--fit', 'on',
    '--n-gpu-layers', '99',

    # KV cache
    '--cache-type-k', 'q8_0',
    '--cache-type-v', 'q8_0',

    # Sampling
    '--temp', '0.7',

    # Attention
    '--flash-attn', 'on',
]
    with (root / 'model.log').open('ab') as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    bridge_environment = None
    try:
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            while process.poll() is None:
                if (read_json(root / 'stop.json') or {}).get('startup_id') == startup:
                    raise RuntimeError('Local model startup was stopped.')
                try:
                    response = client.get(url + '/models', headers={'Authorization': 'Bearer ' + key})
                    if response.is_success and any(
                        entry.get('id') == 'superbot-phone' for entry in response.json().get('data', [])
                    ):
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError(
                                 f"Local model exited before becoming ready "
                                 f"(exit code: {process.returncode}). "
                                 f"See {root / 'model.log'}."
                                )
        if os.getenv('SUPER_BOT_SBP_CONFIG', '').strip():
            # The opt-in Minecraft bridge reuses the selected managed model.
            # Keep the existing non-bridge launcher behavior intact.
            settings = {'PHONE_MODEL_PROVIDER': 'phone', 'PHONE_MODEL_BASE_URL': url,
                        'PHONE_MODEL_NAME': 'superbot-phone', 'TEST_PHONE_API_KEY': key}
            bridge_environment = {name: os.environ.get(name) for name in settings}
            os.environ.update(settings)
        yield process
    finally:
        if bridge_environment is not None:
            for name, value in bridge_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
