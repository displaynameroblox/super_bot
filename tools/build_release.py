"""Build a signed, allowlisted server artifact; never package user data or keys."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from packaging.version import Version


def build(root, output, key_path, version, channel):
    version = str(Version(version))
    if channel == 'stable' and Version(version).is_prerelease:
        raise ValueError('Use development for prereleases')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'super-bot-release.tar.gz'
    files = sorted([*root.joinpath('Ai').glob('*.py'), root / 'Ai' / 'requirements.txt',
                    *root.joinpath('Ai', 'web').glob('*.html'),
                    *root.joinpath('Ai', 'web').glob('*.js'),
                    *root.joinpath('Ai', 'web').glob('*.css')])
    with tarfile.open(archive, 'w:gz') as bundle:
        for path in files:
            if path.is_symlink():
                raise ValueError(f'Refusing symlink: {path.name}')
            raw = path.read_bytes()
            item = tarfile.TarInfo(path.relative_to(root).as_posix())
            item.size, item.mode, item.mtime = len(raw), 0o644, 0
            bundle.addfile(item, io.BytesIO(raw))
    manifest = {'format': 1, 'version': version, 'channel': channel,
                'archive': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'size': archive.stat().st_size, 'python': '>=3.10',
                'platforms': ['darwin', 'linux'],
                'architectures': ['arm64', 'aarch64', 'x86_64', 'amd64', 'armv7l', 'i686', 'x86'],
                'data_schema': 1, 'migrations': [],
                'dependencies': [line for line in root.joinpath('Ai', 'requirements.txt').read_text().splitlines()
                                 if line.strip() and not line.startswith('#')]}
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError('Signing requires an RSA private key of at least 2048 bits')
    output.joinpath('manifest.json').write_bytes(raw)
    output.joinpath('manifest.sig').write_bytes(key.sign(raw, padding.PKCS1v15(), hashes.SHA256()))
    # Keep legacy Termux releases verifiable during migration.
    output.joinpath(archive.name + '.sig').write_bytes(key.sign(archive.read_bytes(), padding.PKCS1v15(), hashes.SHA256()))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--channel', choices=('stable', 'development'), default='stable')
    args = parser.parse_args()
    build(args.root, args.output, args.key, args.version, args.channel)
