"""Isolated release/security tests and actual managed-process lifecycle tests."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes

from Ai.manager_runtime import FileLock, ManagerError, Runtime, atomic_json, locked, read_json
from Ai.releases import Releases, ARCHIVE, extract, fetch, verify_manifest, SafeRedirect
from Ai.manager_cli import main, remote_command
from tools.build_release import build

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.runtime = Runtime(self.temp / 'instance', 5)
        self.releases = Releases(self.runtime)
        self.keypath = self.temp / 'private.pem'
        self.keypath.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.public = self.temp / 'public.pem'
        self.public.write_text(self.pem)
        self.publish = self.temp / 'publish'
        self.manifest = build(ROOT, self.publish, self.keypath, '2.2.0', 'stable')
        self.releases.use('local', self.publish, self.public, True)

    def signed(self, **changes):
        manifest = {**self.manifest, **changes}
        raw = json.dumps(manifest).encode()
        signature = self.key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
        return raw, signature

    def test_local_signed_release_caches_and_reverifies_offline(self):
        self.assertEqual(self.releases.check(), self.manifest)
        with patch('Ai.releases.urllib.request.build_opener', side_effect=AssertionError('network forbidden')):
            self.assertEqual(self.releases.check(offline=True), self.manifest)
        archive = self.releases.cache / (self.manifest['sha256'] + '.tar.gz')
        archive.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ManagerError, 'corrupted'):
            self.releases.check(offline=True)

    def test_bad_signature_and_corrupted_archive_fail_before_stop(self):
        for filename in ('manifest.sig', ARCHIVE):
            original = (self.publish / filename).read_bytes()
            (self.publish / filename).write_bytes(original + b'x')
            with patch.object(self.runtime, 'stop') as stop:
                with self.assertRaises(ManagerError):
                    self.releases.apply()
                stop.assert_not_called()
            (self.publish / filename).write_bytes(original)

    def test_compatibility_and_migrations_fail_closed(self):
        for changes in ({'format': 2}, {'channel': 'development'}, {'python': '>=99'},
                        {'data_schema': 2}, {'migrations': ['rewrite']}, {'platforms': ['alien']},
                        {'architectures': ['alien']}, {'version': 'bad'}, {'size': -1},
                        {'dependencies': ['nonexistent-superbot-test-package>=1']},
                        {'dependencies': ['fastapi>=9999']}, {'archive': '../evil'},
                        {'version': '2.2.0rc1'}):
            with self.subTest(changes=changes), self.assertRaises((ManagerError, ValueError)):
                verify_manifest(*self.signed(**changes), self.pem, 'stable')

    def test_source_trust_is_explicit_and_source_switch_does_not_reuse_keys(self):
        for kwargs in ({}, {'trust': True}, {'trust': True, 'allow_http': True}):
            with self.assertRaises(ManagerError):
                self.releases.use('lan', 'http://127.0.0.1:8001', **kwargs)
        self.releases.use('lan', 'http://127.0.0.1:8001', self.public, True, True)
        self.assertEqual(self.releases.config()['kind'], 'lan')
        with self.assertRaises(ManagerError):
            self.releases.use('lan', 'https://user:secret@example.org', self.public, True)

    def test_official_uses_versioned_github_release_and_channel(self):
        response = [{'draft': False, 'prerelease': True, 'tag_name': 'v3.0.0rc1',
                     'assets': [{'name': n} for n in ('manifest.json', 'manifest.sig', ARCHIVE)]},
                    {'draft': False, 'prerelease': False, 'tag_name': 'v2.2.0',
                     'assets': [{'name': n} for n in ('manifest.json', 'manifest.sig', ARCHIVE)]}]
        with patch('Ai.releases.fetch', return_value=json.dumps(response).encode()) as get:
            url = self.releases.base({'kind': 'official', 'channel': 'stable'})
            self.assertTrue(url.endswith('/releases/download/v2.2.0'))
            self.assertIn('api.github.com/repos/displaynameroblox/super_bot', get.call_args.args[0])
            self.assertTrue(self.releases.base({'kind': 'official', 'channel': 'development'}).endswith('v3.0.0rc1'))

    def test_timeout_and_permission_errors_leave_installation_untouched(self):
        for failure in (TimeoutError('test'), PermissionError('test')):
            with patch('Ai.releases.fetch', side_effect=failure), patch.object(self.runtime, 'stop') as stop:
                with self.assertRaises(type(failure)):
                    self.releases.apply()
                stop.assert_not_called()
                self.assertIsNone(self.releases.active())

    def test_downgrade_refused(self):
        atomic_json(self.runtime.root / 'active.json', {'version': '99.0.0'})
        with self.assertRaisesRegex(ManagerError, 'Downgrade'):
            self.releases.check()

    def test_traversal_links_duplicates_and_windows_paths_rejected(self):
        for name, kind in [('Ai/../outside', tarfile.REGTYPE), ('/Ai/evil', tarfile.REGTYPE),
                           ('Ai/\\outside', tarfile.REGTYPE), ('Ai/C:evil', tarfile.REGTYPE),
                           ('Ai/CON.txt', tarfile.REGTYPE), ('Ai/file.', tarfile.REGTYPE),
                           ('Ai/link', tarfile.SYMTYPE), ('Ai/hard', tarfile.LNKTYPE),
                           ('Ai/dir', tarfile.DIRTYPE), ('Ai//x', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                archive = self.temp / 'evil.tar.gz'
                with tarfile.open(archive, 'w:gz') as bundle:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    bundle.addfile(member)
                with self.assertRaises(ManagerError):
                    extract(archive, self.temp / 'tree')

    def test_activation_failure_restores_data_and_previous_pointer(self):
        candidate = self.releases.check()
        self.releases.stage(candidate)
        data = self.runtime.root / 'data'
        data.mkdir()
        (data / 'identity').write_text('original')
        (self.runtime.root / '.env').write_text('PRIVATE=value')
        client = self.temp / 'client.db'
        client.write_bytes(b'client history')
        calls = []
        def start(source=None, build='installed'):
            calls.append(build)
            if build != 'installed':
                (data / 'identity').write_text('changed')
                raise ManagerError('health', 'failed health')
            self.assertEqual((data / 'identity').read_text(), 'original')
        with patch.object(self.runtime, 'status', return_value={'state': 'healthy'}), \
             patch.object(self.runtime, 'stop'), patch.object(self.runtime, 'start', side_effect=start):
            with self.assertRaisesRegex(ManagerError, 'failed health'):
                self.releases.activate(candidate)
        self.assertEqual(calls, [candidate['sha256'], 'installed'])
        self.assertIsNone(self.releases.active())
        self.assertEqual(client.read_bytes(), b'client history')
        self.assertEqual((self.runtime.root / '.env').read_text(), 'PRIVATE=value')
        self.assertFalse((self.runtime.root / 'transaction.json').exists())

    def test_interrupted_activation_recovery(self):
        data = self.runtime.root / 'data'
        data.mkdir()
        (data / 'db').write_bytes(b'original')
        backup = self.releases.backup()
        (data / 'db').write_bytes(b'partial migration')
        atomic_json(self.runtime.root / 'transaction.json', {'previous': None, 'backup': backup, 'was_running': False})
        self.releases.recover()
        self.assertEqual((data / 'db').read_bytes(), b'original')
        self.assertFalse((self.runtime.root / 'transaction.json').exists())

    def test_atomic_permission_failure_preserves_pointer(self):
        path = self.runtime.root / 'test.json'
        atomic_json(path, {'original': True})
        with patch('Ai.manager_runtime.os.replace', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                atomic_json(path, {'original': False})
        self.assertEqual(read_json(path), {'original': True})

    def test_update_lock_prevents_parallel_mutations(self):
        with FileLock(self.runtime.root / 'operation.lock'):
            with contextlib.redirect_stderr(io.StringIO()) as output:
                code = main(['--instance', str(self.runtime.root), 'update', 'apply', '--offline'])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())['error']['code'], 'busy')

    def test_backup_failure_restarts_previous_before_returning_error(self):
        self.releases.check()
        self.releases.stage(self.manifest)
        with patch.object(self.runtime, 'status', return_value={'state': 'healthy'}), \
             patch.object(self.runtime, 'stop'), patch.object(self.runtime, 'start') as start, \
             patch.object(self.releases, 'backup', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.releases.activate(self.manifest)
            start.assert_called_once_with(None, 'installed')

    def test_https_redirect_cannot_downgrade(self):
        from urllib.request import Request
        with self.assertRaises(ManagerError):
            SafeRedirect().redirect_request(Request('https://example.org/release'), None, 302, '', {}, 'http://example.org/release')

    def test_init_preserves_identity_and_refuses_overwrite(self):
        source = self.temp / 'old-server'
        source.mkdir()
        (source / 'identity').write_bytes(b'identity fixture')
        config = self.temp / 'old.env'
        config.write_text('HOST=127.0.0.1')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--instance', str(self.runtime.root), 'init', '--server-data', str(source), '--config', str(config)]), 0)
            self.assertEqual(main(['--instance', str(self.runtime.root), 'init', '--server-data', str(source)]), 1)
        self.assertEqual((self.runtime.root / 'data' / 'identity').read_bytes(), b'identity fixture')
        self.assertEqual((source / 'identity').read_bytes(), b'identity fixture')

    def test_archive_size_limit(self):
        (self.temp / 'oversize').write_bytes(b'12345')
        with self.assertRaises(ManagerError):
            fetch(self.temp / 'oversize', 4)

    def test_remote_command_has_strict_identity_and_no_shell_input(self):
        cmd = remote_command('user@host', 'status')
        self.assertIn('StrictHostKeyChecking=yes', cmd)
        self.assertIn('BatchMode=yes', cmd)
        for host in ('-oProxyCommand=evil', 'host;echo evil', 'host\ncommand', '$(evil)'):
            with self.assertRaises(ManagerError):
                remote_command(host, 'status')

    def test_admin_noninteractive_snapshot(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--instance', str(self.runtime.root), 'admin', '--once'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['server']['state'], 'stopped')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'instance'
        self.runtime = Runtime(self.root, 8)
        self.port = free_port()
        self.env = patch.dict(os.environ, {'PORT': str(self.port), 'HOST': '127.0.0.1',
                  'DISCOVERY_PORT': str(free_port()), 'PHONE_MODEL_PROVIDER': 'phone',
                  'PHONE_MODEL_BASE_URL': 'http://127.0.0.1:1/v1', 'PHONE_MODEL_NAME': 'superbot-phone',
                  'TEST_PHONE_API_KEY': 'fixture', 'SSL_CERTFILE': '', 'SSL_KEYFILE': '',
                  'SUPER_BOT_REQUIRE_MODEL_ON_START': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.runtime.stop)

    def test_offline_start_duplicate_stop_restart_and_private_stats(self):
        state = self.runtime.start()
        self.assertEqual(state['state'], 'healthy')
        self.assertNotIn('startup_id', state)
        first = read_json(self.root / 'process.json')['startup_id']
        with self.assertRaisesRegex(ManagerError, 'already running'):
            self.runtime.start()
        self.runtime.stop()
        self.assertEqual(self.runtime.status()['state'], 'stopped')
        self.assertEqual(self.runtime.start()['state'], 'healthy')
        self.assertNotEqual(read_json(self.root / 'process.json')['startup_id'], first)

    def test_occupied_port_never_counts_as_success(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            with self.assertRaisesRegex(ManagerError, 'did not become ready'):
                self.runtime.start()
        self.assertEqual(self.runtime.status()['state'], 'stopped')
        self.assertIn('already be using this port', (self.root / 'server.log').read_text())

    def test_failed_readiness_cleans_child(self):
        with patch('Ai.manager_runtime.health', return_value=False):
            self.runtime.timeout = 1
            with self.assertRaises(ManagerError):
                self.runtime.start()
        self.assertFalse(locked(self.root / 'server.lock'))

    def test_lock_is_released_after_process_crash(self):
        script = "from Ai.manager_runtime import FileLock; import sys,time; lock=FileLock(sys.argv[1]); lock.__enter__(); print('ready',flush=True); time.sleep(60)"
        process = subprocess.Popen([sys.executable, '-c', script, str(self.root / 'crash.lock')], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.assertTrue(locked(self.root / 'crash.lock'))
        finally:
            process.kill()
            process.wait()
            process.stdout.close()
        self.assertFalse(locked(self.root / 'crash.lock'))

    def test_real_signed_upgrade_and_rollback(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = self.root / 'key.pem'
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        public = self.root / 'public.pem'
        public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        publish = self.root / 'publish'
        manifest = build(ROOT, publish, private, '2.2.0', 'stable')
        releases = Releases(self.runtime)
        releases.use('local', publish, public, True)
        self.runtime.start()
        identity_files = {p.name: p.read_bytes() for p in (self.root / 'data').iterdir() if p.is_file()}
        self.assertEqual(releases.apply()['state'], 'updated')
        self.assertEqual(self.runtime.status()['build'], manifest['sha256'])
        # A signed candidate that reports the wrong identity must automatically restore the running release.
        candidate_root = self.root / 'candidate-source'
        import shutil
        (candidate_root / 'Ai').mkdir(parents=True)
        for path in (ROOT / 'Ai').glob('*.py'):
            shutil.copy2(path, candidate_root / 'Ai' / path.name)
        shutil.copy2(ROOT / 'Ai' / 'requirements.txt', candidate_root / 'Ai' / 'requirements.txt')
        gateway = candidate_root / 'Ai' / 'gateway.py'
        gateway.write_text(gateway.read_text().replace('"status": "ok", "build": build', '"status": "wrong", "build": build'))
        build(candidate_root, publish, private, '3.0.0', 'stable')
        self.runtime.timeout = 4
        with self.assertRaisesRegex(ManagerError, 'did not become ready'):
            releases.apply()
        self.assertEqual(self.runtime.status()['state'], 'healthy')
        self.assertEqual(self.runtime.status()['build'], manifest['sha256'])
        self.assertEqual(releases.rollback()['state'], 'rolled_back')
        self.assertEqual(self.runtime.status()['build'], 'installed')
        self.assertEqual(identity_files, {p.name: p.read_bytes() for p in (self.root / 'data').iterdir() if p.is_file()})


class NetworkReleaseTests(unittest.TestCase):
    """Real loopback HTTP transport; no personal LAN or external release service."""
    setUpClass = classmethod(ReleaseTests.setUpClass.__func__)
    setUp = ReleaseTests.setUp
    def test_lan_and_interrupted_download(self):
        import functools
        import http.server
        import threading
        class Handler(http.server.SimpleHTTPRequestHandler):
            interrupt = False
            def log_message(self, *_):
                pass
            def do_GET(self):
                if self.interrupt and self.path.endswith(ARCHIVE):
                    self.send_response(200)
                    self.send_header('Content-Length', '1000')
                    self.end_headers()
                    self.wfile.write(b'partial')
                    self.close_connection = True
                else:
                    super().do_GET()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(self.publish)))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.releases.use('lan', f'http://127.0.0.1:{server.server_port}', self.public, True, True)
            self.assertEqual(self.releases.check(), self.manifest)
            for archive in self.releases.cache.glob('*.tar.gz'):
                archive.unlink()
            Handler.interrupt = True
            with self.assertRaises((ManagerError, OSError)):
                self.releases.check()
            self.assertIsNone(self.releases.active())
            self.assertEqual(list(self.releases.cache.glob('*.tar.gz')), [])
        finally:
            server.shutdown()
            worker.join()
            server.server_close()
