"""Server administration commands; legacy model/library commands remain available."""
from __future__ import annotations
import argparse
from importlib import metadata
import json
import http.client
import tarfile
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from Ai.manager_runtime import FileLock, ManagerError, Runtime, read_json
from Ai.releases import Releases

COMMANDS = {'init', 'start', 'stop', 'restart', 'status', 'logs', 'source', 'update', 'admin', 'remote', 'config'}


def parser():
    from Ai.local_manager import data_directory
    p = argparse.ArgumentParser(prog='super-bot-manager', description=__doc__)
    p.add_argument('--instance', type=Path, default=data_directory() / 'instances' / 'default')
    p.add_argument('--json', action='store_true', help='machine-readable result and errors')
    p.add_argument('--timeout', type=float, default=45, help='startup/shutdown deadline in seconds')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='copy existing configuration and identity into a new instance')
    init.add_argument('--config', type=Path)
    init.add_argument('--server-data', type=Path)
    start = sub.add_parser('start', help='start the installed server without checking the network')
    start.add_argument('--foreground', action='store_true')
    for command in ('stop', 'restart', 'status', 'config'):
        sub.add_parser(command)
    logs = sub.add_parser('logs')
    logs.add_argument('--follow', action='store_true')
    logs.add_argument('--lines', type=int, default=40)
    source = sub.add_parser('source').add_subparsers(dest='action', required=True)
    source.add_parser('list')
    use = source.add_parser('use')
    use.add_argument('kind', choices=('official', 'local', 'lan'))
    use.add_argument('location', nargs='?')
    use.add_argument('--channel', choices=('stable', 'development'), default='stable')
    use.add_argument('--key', type=Path)
    use.add_argument('--trust-key', action='store_true')
    use.add_argument('--allow-http', action='store_true')
    update = sub.add_parser('update').add_subparsers(dest='action', required=True)
    for command in ('check', 'apply'):
        update.add_parser(command).add_argument('--offline', action='store_true')
    update.add_parser('rollback')
    admin = sub.add_parser('admin')
    admin.add_argument('--plain', action='store_true', help='line-oriented keyboard menu')
    admin.add_argument('--once', action='store_true', help='print one snapshot and exit')
    remote = sub.add_parser('remote', help='run an allowlisted command over verified SSH')
    remote.add_argument('host', help='SSH host or configured SSH alias (known_hosts verification required)')
    remote.add_argument('action', choices=('status', 'start', 'stop', 'restart', 'logs', 'check', 'apply', 'rollback'))
    return p


def remote_command(host, action):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host):
        raise ManagerError('remote', 'Use a hostname or SSH alias; configure ports and keys in your SSH config.')
    actions = {name: name for name in ('status', 'start', 'stop', 'restart', 'logs')}
    actions.update({name: 'update ' + name for name in ('check', 'apply', 'rollback')})
    return ['ssh', '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            host, 'super-bot-manager --json ' + actions[action]]


def snapshot(runtime, releases):
    source = {k: v for k, v in releases.config().items() if k != 'key'}
    return {'server': runtime.status(), 'release': releases.active() or {'version': metadata.version('super-bot')},
            'source': source, 'instance': str(runtime.root),
            'selected_model': (read_json(runtime.root / 'model.json') or {}).get('path'),
            'model_log': str(runtime.root / 'model.log'),
            'recovery_pending': (runtime.root / 'transaction.json').exists()}


def logs(runtime, lines=40):
    if not 1 <= lines <= 10000:
        raise ManagerError('arguments', '--lines must be between 1 and 10000.', 2)
    path = runtime.root / 'server.log'
    if not path.exists():
        return ''
    with path.open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 256 * 1024))
        return '\n'.join(stream.read().decode('utf-8', errors='replace').splitlines()[-lines:])


def execute(args, runtime, releases):
    command = args.command
    if command == 'status':
        return snapshot(runtime, releases)
    if command == 'config':
        # Deliberate allowlist; never dump .env, provider keys, or credentials.
        return {'instance': str(runtime.root), 'config_file': str(runtime.root / '.env'),
                'data': str(runtime.root / 'data'), 'local_only_default': True,
                'source': {k: v for k, v in releases.config().items() if k != 'key'}}
    if command == 'logs':
        return {'log': logs(runtime, args.lines)}
    if command == 'source' and args.action == 'list':
        return {'available': ['official', 'local', 'lan'],
                'selected': {k: v for k, v in releases.config().items() if k != 'key'}}
    with FileLock(runtime.root / 'operation.lock'):
        if command == 'init':
            import shutil
            from Ai.manager_runtime import locked
            if locked(runtime.root / 'server.lock') or (runtime.root / 'data').exists() or (runtime.root / '.env').exists():
                raise ManagerError('exists', 'Instance already contains data or is running; nothing was overwritten.')
            if args.config and not args.config.expanduser().is_file():
                raise ManagerError('config', 'Configuration file does not exist.')
            if args.server_data and not args.server_data.expanduser().is_dir():
                raise ManagerError('config', 'Server data directory does not exist.')
            if args.server_data:
                shutil.copytree(args.server_data.expanduser(), runtime.root / 'data')
            if args.config:
                shutil.copy2(args.config.expanduser(), runtime.root / '.env')
                (runtime.root / '.env').chmod(0o600)
            return {'state': 'initialized', 'instance': str(runtime.root)}
        if command == 'source':
            return releases.use(args.kind, args.location, args.key, args.trust_key, args.allow_http, args.channel)
        if command == 'update':
            if args.action == 'rollback':
                return releases.rollback()
            return getattr(releases, args.action)(args.offline)
        if command in ('start', 'restart'):
            from Ai.managed_model import check_model_ready
            try:
                check_model_ready(runtime.root)
            except RuntimeError as exc:
                raise ManagerError('model_setup', str(exc), 4) from exc
        if command == 'start':
            return releases.start()
        if command == 'stop':
            return runtime.stop()
        if command == 'restart':
            runtime.stop()
            return releases.start()
    raise ManagerError('arguments', 'Unknown manager action.', 2)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 0 < args.timeout <= 600:
            raise ManagerError('arguments', '--timeout must be greater than zero and at most 600.', 2)
        if args.command == 'remote':
            result = subprocess.run(remote_command(args.host, args.action), timeout=600)
            return result.returncode
        runtime = Runtime(args.instance, args.timeout)
        releases = Releases(runtime)
        if args.command == 'admin':
            from Ai.admin import run
            return run(args, runtime, releases)
        result = execute(args, runtime, releases)
        if args.command == 'start' and args.foreground:
            print(json.dumps(result, indent=2), flush=True)
            try:
                while runtime.status()['state'] != 'stopped':
                    time.sleep(.5)
            except KeyboardInterrupt:
                with FileLock(runtime.root / 'operation.lock'):
                    runtime.stop()
            return 0
        if args.command == 'logs' and args.follow:
            while True:
                print(result['log'], flush=True)
                path = runtime.root / 'server.log'
                offset = path.stat().st_size if path.exists() else 0
                while not path.exists() or path.stat().st_size == offset:
                    time.sleep(.5)
                with path.open('rb') as stream:
                    stream.seek(offset if path.stat().st_size >= offset else 0)
                    result = {'log': stream.read().decode(errors='replace')}
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (ManagerError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, http.client.HTTPException, tarfile.TarError) as exc:
        # URLs may contain private paths but never credentials; do not echo .env or exception payloads.
        code = getattr(exc, 'code', 'operation_failed')
        message = str(exc) if isinstance(exc, ManagerError) else f'{type(exc).__name__}: operation failed; check configuration, permissions and connectivity.'
        print(json.dumps({'error': {'code': code, 'message': message}}), file=sys.stderr)
        return getattr(exc, 'exit_code', 1)


if __name__ == '__main__':
    raise SystemExit(main())
