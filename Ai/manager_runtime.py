"""Private, local process control. No network administration endpoints."""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time


class ManagerError(RuntimeError):
    def __init__(self, code, message, exit_code=1):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


class FileLock:
    """Kernel-owned lock: released on crash; never delete the lock inode."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0, 2)
                if not self.stream.tell():
                    self.stream.write(b'\0')
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ManagerError('busy', 'Another manager or server owns this instance.', 3) from exc
        return self

    def __exit__(self, *_):
        if os.name == 'nt':
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.stream.close()


def locked(path):
    try:
        with FileLock(path):
            return False
    except ManagerError:
        return True


def health(state):
    """Pin the local certificate before sending HTTP; verify launch identity."""
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(state['probe_host'], state['port'], timeout=1, context=context)
        try:
            connection.connect()
            fingerprint = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if fingerprint != state['tls_fingerprint']:
                return False
            connection.request('GET', '/health')
            response = connection.getresponse()
            payload = json.loads(response.read(4096))
            return response.status == 200 and payload == {
                'status': 'ok', 'build': state['build'], 'startup_id': state['startup_id']}
        finally:
            connection.close()
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return False


class Runtime:
    def __init__(self, root, timeout=45):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.timeout = timeout

    def status(self):
        state = read_json(self.root / 'process.json', {})
        running = locked(self.root / 'server.lock')
        result = {k: v for k, v in state.items() if k != 'startup_id'}
        result['state'] = ('healthy' if health(state) else 'unhealthy') if running else 'stopped'
        telemetry = read_json(self.root / 'telemetry.json', {}) if running else {}
        if telemetry.get('startup_id') == state.get('startup_id'):
            result.update({k: telemetry[k] for k in ('clients', 'sessions') if k in telemetry})
        result['log'] = str(self.root / 'server.log')
        return result

    def start(self, source=None, build='installed', foreground=False):
        if locked(self.root / 'server.lock'):
            raise ManagerError('already_running', 'This instance is already running.', 3)
        self.root.joinpath('stop.json').unlink(missing_ok=True)
        self.root.joinpath('process.json').unlink(missing_ok=True)
        launch = secrets.token_hex(32)
        bootstrap = str(Path(__file__).resolve().parent.parent)
        command = [sys.executable, '-E', '-B', '-c',
                   'import sys; sys.path.insert(0, sys.argv.pop(1)); from Ai.manager_worker import main; main()',
                   bootstrap, str(self.root), str(source or bootstrap), build, launch]
        # Carry configuration from the launch folder into the managed child.
        # Instance configuration and explicit environment values take precedence.
        from dotenv import dotenv_values
        environment = {
            **{k: v for k, v in dotenv_values(Path.cwd() / '.env').items() if v is not None},
            **{k: v for k, v in dotenv_values(self.root / '.env').items() if v is not None},
            **os.environ,
        }
        if environment.get('SUPER_BOT_PORT'):
            environment['PORT'] = environment['SUPER_BOT_PORT']
        environment.update(SUPER_BOT_STARTUP_ID=launch, SUPER_BOT_MANAGED='1',
                           SUPER_BOT_SERVER_DIR=str(self.root / 'data'))
        if os.name == 'nt':
            options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            options = {'start_new_session': not foreground}
        log = (self.root / 'server.log').open('ab')
        try:
            process = subprocess.Popen(command, cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, **options)
        finally:
            log.close()
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline and process.poll() is None:
                state = read_json(self.root / 'process.json', {})
                if state.get('startup_id') == launch and health(state):
                    if foreground:
                        try:
                            return {'exit_code': process.wait()}
                        except KeyboardInterrupt:
                            self.stop()
                            return {'state': 'stopped'}
                    return self.status()
                time.sleep(.1)
            raise ManagerError('startup_failed', f'Server did not become ready. See {self.root / "server.log"}', 4)
        except BaseException:
            if process.poll() is None:
                atomic_json(self.root / 'stop.json', {'startup_id': launch})
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            raise

    def stop(self):
        if not locked(self.root / 'server.lock'):
            return {'state': 'stopped'}
        state = read_json(self.root / 'process.json', {})
        if not state.get('startup_id'):
            raise ManagerError('starting', 'Server is still initializing; retry stop shortly.', 3)
        atomic_json(self.root / 'stop.json', {'startup_id': state['startup_id']})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if not locked(self.root / 'server.lock'):
                return {'state': 'stopped'}
            time.sleep(.1)
        raise ManagerError('stop_timeout', 'Graceful shutdown timed out; update was not activated.', 4)
