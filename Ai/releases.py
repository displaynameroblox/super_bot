"""Signed, bounded release acquisition and transactional activation.

Trust anchors are configured independently of release sources. The manifest is
RSA/PKCS1v15/SHA256 signed, as in the existing Termux release tooling.
"""
from __future__ import annotations

import base64
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from Ai.manager_runtime import ManagerError, atomic_json, read_json

REPOSITORY = 'displaynameroblox/super_bot'
ARCHIVE = 'super-bot-release.tar.gz'
MAX_BYTES = 64 * 1024 * 1024
BUILD = re.compile(r'[0-9a-f]{64}')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme not in ('http', 'https') or (old.scheme == 'https' and new.scheme != 'https'):
            raise ManagerError('redirect', 'Release redirect would weaken transport security.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(location, limit=MAX_BYTES):
    """Bound reads and time; no ambient proxy or credential configuration."""
    parsed = urlsplit(str(location))
    if parsed.scheme not in ('https', 'http'):
        with Path(location).open('rb') as stream:
            content = stream.read(limit + 1)
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect())
        request = urllib.request.Request(str(location), headers={'User-Agent': 'SuperBot-Manager/1'})
        deadline = time.monotonic() + 120
        chunks, total = [], 0
        with opener.open(request, timeout=10) as response:
            while chunk := response.read1(65536):
                total += len(chunk)
                if total > limit or time.monotonic() > deadline:
                    raise ManagerError('download_limit', 'Release download exceeded time or size limit.')
                chunks.append(chunk)
        content = b''.join(chunks)
    if len(content) > limit:
        raise ManagerError('download_limit', 'Release exceeds size limit.')
    return content


def verify_manifest(raw, signature, key_pem, channel):
    key = serialization.load_pem_public_key(key_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ManagerError('trust', 'A trusted RSA public key of at least 2048 bits is required.')
    try:
        key.verify(signature, raw, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise ManagerError('signature', 'Release manifest signature is invalid.') from exc
    manifest = json.loads(raw)
    if manifest.get('format') != 1 or manifest.get('channel') != channel:
        raise ManagerError('manifest', 'Unsupported manifest format or release channel.')
    Version(manifest['version'])
    if channel == 'stable' and Version(manifest['version']).is_prerelease:
        raise ManagerError('manifest', 'Prerelease versions are not allowed on stable.')
    if manifest.get('archive') != ARCHIVE or not BUILD.fullmatch(manifest.get('sha256', '')):
        raise ManagerError('manifest', 'Invalid archive identity.')
    if not isinstance(manifest.get('size'), int) or not 0 < manifest['size'] <= MAX_BYTES:
        raise ManagerError('manifest', 'Invalid archive size.')
    if manifest.get('data_schema') != 1 or manifest.get('migrations') != []:
        raise ManagerError('migration', 'This manager cannot safely apply this data migration; release rejected.')
    if Version(platform.python_version()) not in SpecifierSet(manifest['python']):
        raise ManagerError('compatibility', 'This release does not support the installed Python.')
    if platform.system().lower() not in manifest['platforms'] or platform.machine().lower() not in manifest['architectures']:
        raise ManagerError('compatibility', 'This release does not support this platform or architecture.')
    for text in manifest['dependencies']:
        requirement = Requirement(text)
        if requirement.url:
            raise ManagerError('dependencies', 'Direct URL dependencies are not supported.')
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError as exc:
            raise ManagerError('dependencies', f'Missing dependency: {requirement.name}. Upgrade the bootstrap package first.') from exc
        if installed not in requirement.specifier:
            raise ManagerError('dependencies', f'Incompatible dependency: {requirement.name}. Upgrade the bootstrap package first.')
    return manifest


def extract(archive, tree):
    remaining = MAX_BYTES
    names = set()
    with tarfile.open(archive, 'r:gz') as bundle:
        for index, member in enumerate(bundle):
            path = PurePosixPath(member.name)
            # Reject Windows drives, backslashes, alternate streams and reserved names on every OS.
            reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
            if (index >= 2048 or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                    or path.is_absolute() or len(path.parts) < 2 or path.parts[0] != 'Ai'
                    or '\\' in member.name or ':' in member.name
                    or member.name != path.as_posix()
                    or any(p.startswith('.') or p.endswith((' ', '.')) or p.split('.')[0].lower() in reserved for p in path.parts)
                    or path.as_posix().casefold() in names or member.size < 0):
                raise ManagerError('archive', f'Invalid release member: {member.name!r}')
            names.add(path.as_posix().casefold())
            remaining -= member.size
            if remaining < 0:
                raise ManagerError('archive', 'Expanded release exceeds size limit.')
            target = tree.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
    for name in ('__init__.py', 'server.py', 'gateway.py'):
        if not (tree / 'Ai' / name).is_file():
            raise ManagerError('archive', f'Release is missing Ai/{name}.')


class Releases:
    def __init__(self, runtime):
        self.runtime, self.root = runtime, runtime.root
        self.cache = self.root / 'releases'
        self.cache.mkdir(exist_ok=True, mode=0o700)

    def config(self):
        return read_json(self.root / 'source.json', {'kind': 'official', 'channel': 'stable'})

    def use(self, kind, location=None, key=None, trust=False, allow_http=False, channel='stable'):
        if kind not in ('official', 'local', 'lan') or channel not in ('stable', 'development'):
            raise ManagerError('source', 'Unsupported source or channel.')
        config = {'kind': kind, 'channel': channel}
        if kind == 'local':
            if not location or not Path(location).expanduser().is_dir():
                raise ManagerError('source', 'Choose an existing release directory.')
            config['location'] = str(Path(location).expanduser().resolve())
        elif kind == 'lan':
            parsed = urlsplit(location or '')
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):
                raise ManagerError('source', 'Choose a plain http(s) release URL without credentials or query strings.')
            if not trust or (parsed.scheme == 'http' and not allow_http):
                raise ManagerError('trust', 'LAN sources need --trust-key and HTTP additionally needs --allow-http.')
            config['location'] = location.rstrip('/')
            config['allow_http'] = allow_http
        if key:
            if not trust:
                raise ManagerError('trust', 'Verify the public key fingerprint independently, then use --trust-key.')
            pem = Path(key).expanduser().read_text(encoding='ascii')
            public = serialization.load_pem_public_key(pem.encode())
            if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
                raise ManagerError('trust', 'Expected an RSA public key of at least 2048 bits.')
            config['key'] = pem
        elif kind == 'official':
            pinned = Path(__file__).with_name('release-public.pem')
            if pinned.is_file():
                config['key'] = pinned.read_text(encoding='ascii')
        if 'key' not in config:
            raise ManagerError('trust', 'No trusted signing key is installed. Supply --key PATH --trust-key after independently verifying it.')
        config['key_fingerprint'] = hashlib.sha256(config['key'].encode()).hexdigest()
        atomic_json(self.root / 'source.json', config)
        return {k: v for k, v in config.items() if k != 'key'}

    def base(self, config):
        if config['kind'] != 'official':
            return config['location']
        endpoint = f'https://api.github.com/repos/{REPOSITORY}/releases'
        releases = json.loads(fetch(endpoint, 1024 * 1024))
        for release in releases:
            if release['draft'] or (config['channel'] == 'stable' and release['prerelease']):
                continue
            tag = release['tag_name']
            if not re.fullmatch(r'v[0-9][A-Za-z0-9.+-]*', tag):
                continue
            names = {asset['name'] for asset in release['assets']}
            if {'manifest.json', 'manifest.sig', ARCHIVE} <= names:
                return f'https://github.com/{REPOSITORY}/releases/download/{tag}'
        raise ManagerError('release_missing', 'No signed release is published for this channel.')

    def check(self, offline=False):
        config = self.config()
        key = config.get('key')
        if not key:
            pinned = Path(__file__).with_name('release-public.pem')
            key = pinned.read_text(encoding='ascii') if pinned.is_file() else None
        if not key:
            raise ManagerError('trust', 'Official signing key is not provisioned; see the release setup guide.')
        if offline:
            cached = read_json(self.root / 'checked.json')
            if not cached:
                raise ManagerError('cache', 'No verified update is cached.')
            raw, signature = base64.b64decode(cached['raw']), base64.b64decode(cached['signature'])
            base = None
        else:
            base = self.base(config)
            raw = fetch(f'{base}/manifest.json', 65536)
            signature = fetch(f'{base}/manifest.sig', 16384)
        manifest = verify_manifest(raw, signature, key, config['channel'])
        active = read_json(self.root / 'active.json')
        current = active['version'] if active else metadata.version('super-bot')
        if Version(manifest['version']) < Version(current):
            raise ManagerError('downgrade', 'Downgrade rejected. Use update rollback for the previous verified release.')
        if active and config['channel'] == 'stable' and Version(manifest['version']) == Version(current) and manifest['sha256'] != active['sha256']:
            raise ManagerError('immutable', 'A stable version cannot be replaced by different content.')
        archive = self.cache / (manifest['sha256'] + '.tar.gz')
        if not archive.is_file() or hashlib.sha256(fetch(archive)).hexdigest() != manifest['sha256']:
            if offline:
                raise ManagerError('cache', 'Cached archive is missing or corrupted.')
            content = fetch(f'{base}/{ARCHIVE}')
            if len(content) != manifest['size'] or hashlib.sha256(content).hexdigest() != manifest['sha256']:
                raise ManagerError('integrity', 'Release archive integrity check failed.')
            fd, pending = tempfile.mkstemp(dir=self.cache)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(pending, archive)
            finally:
                Path(pending).unlink(missing_ok=True)
        if archive.stat().st_size != manifest['size']:
            raise ManagerError('integrity', 'Cached archive size mismatch.')
        atomic_json(self.root / 'checked.json', {'raw': base64.b64encode(raw).decode(),
                                               'signature': base64.b64encode(signature).decode()})
        return manifest

    def stage(self, manifest):
        build = manifest['sha256']
        destination = self.cache / build
        if destination.exists():
            # Never trust previously extracted executable code; extract anew and compare.
            shutil.rmtree(destination)
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=self.cache) as directory:
            tree = Path(directory) / 'tree'
            tree.mkdir()
            extract(self.cache / (build + '.tar.gz'), tree)
            tree.joinpath('Ai', '.release-build').write_text(build, encoding='ascii')
            tree.rename(destination)
        return destination

    def active(self):
        return read_json(self.root / 'active.json')

    def start(self, foreground=False):
        recovering = (self.root / 'transaction.json').exists()
        self.recover()
        if recovering and self.runtime.status()['state'] == 'healthy':
            return self.runtime.status()
        active = self.active()
        return self.runtime.start(self.source(active), active['sha256'] if active else 'installed', foreground)

    def source(self, release):
        if release is None:
            return None
        build = release.get('sha256', '')
        if not BUILD.fullmatch(build):
            raise ManagerError('pointer', 'Invalid release pointer.')
        source = self.cache / build
        if not source.joinpath('Ai', 'server.py').is_file():
            raise ManagerError('pointer', 'Installed release is missing; restore from a verified cache.')
        return source

    def backup(self):
        target = self.root / 'backups' / (str(time.time_ns()))
        target.mkdir(parents=True, mode=0o700)
        data = self.root / 'data'
        if data.exists():
            shutil.copytree(data, target / 'data')
        if (self.root / '.env').exists():
            shutil.copy2(self.root / '.env', target / '.env')
        return str(target)

    def restore(self, backup):
        backup = Path(backup).resolve()
        if backup.parent != (self.root / 'backups').resolve():
            raise ManagerError('backup', 'Invalid recovery backup path.')
        if not backup.is_dir():
            raise ManagerError('backup', 'Recovery backup is missing.')
        # Only manager-owned server state; never client databases or user libraries.
        data = self.root / 'data'
        if data.exists():
            quarantine = self.root / ('failed-data-' + str(time.time_ns()))
            data.rename(quarantine)
        if (backup / 'data').exists():
            shutil.copytree(backup / 'data', data)
        if (backup / '.env').exists():
            shutil.copy2(backup / '.env', self.root / '.env')

    def recover(self):
        journal = read_json(self.root / 'transaction.json')
        if not journal:
            return
        self.runtime.stop()
        self.restore(journal['backup'])
        atomic_json(self.root / 'active.json', journal['previous'])
        if 'previous_pointer' in journal:
            atomic_json(self.root / 'previous.json', journal['previous_pointer'])
        if journal['was_running']:
            previous = journal['previous']
            self.runtime.start(self.source(previous), previous['sha256'] if previous else 'installed')
        self.root.joinpath('transaction.json').unlink()

    def activate(self, manifest, rollback=False):
        self.recover()
        previous = self.active()
        if previous == manifest:
            return {'state': 'unchanged', 'version': manifest['version'] if manifest else 'installed'}
        was_running = self.runtime.status()['state'] != 'stopped'
        self.runtime.stop()
        try:
            backup = self.backup()
            journal = {'previous': previous, 'backup': backup, 'was_running': was_running,
                       'previous_pointer': read_json(self.root / 'previous.json')}
            atomic_json(self.root / 'transaction.json', journal)
        except BaseException:
            if was_running:
                self.runtime.start(self.source(previous), previous['sha256'] if previous else 'installed')
            raise
        try:
            self.runtime.start(self.source(manifest), manifest['sha256'] if manifest else 'installed')
            if not was_running:
                self.runtime.stop()
            atomic_json(self.root / 'previous.json', {'release': previous, 'backup': backup})
            atomic_json(self.root / 'active.json', manifest)
            self.root.joinpath('transaction.json').unlink()
        except BaseException:
            self.recover()
            raise
        return {'state': 'rolled_back' if rollback else 'updated',
                'version': manifest['version'] if manifest else 'installed', 'backup': backup}

    def apply(self, offline=False):
        self.recover()
        manifest = self.check(offline)
        if self.active() and self.active()['sha256'] == manifest['sha256']:
            return {'state': 'unchanged', 'version': manifest['version']}
        self.stage(manifest)
        return self.activate(manifest)

    def rollback(self):
        self.recover()
        previous = read_json(self.root / 'previous.json')
        if previous is None:
            raise ManagerError('rollback', 'No previous release is available.')
        # All accepted releases use schema 1 and may not run migrations. Preserve current state.
        release = previous['release']
        if release:
            archive = self.cache / (release['sha256'] + '.tar.gz')
            if not archive.is_file() or hashlib.sha256(fetch(archive)).hexdigest() != release['sha256']:
                raise ManagerError('integrity', 'Previous release cache is missing or corrupted; rollback refused.')
            self.stage(release)
        return self.activate(release, rollback=True)
