"""Package previously built artifacts; never publishes or bundles Minecraft itself."""
import hashlib
from pathlib import Path
import shutil
import zipfile
root = Path(__file__).resolve().parents[1]
version = '0.1.0-alpha.3'
out = root / 'build/release' / version
out.mkdir(parents=True, exist_ok=True)
for module, pattern in [('common', 'libs/*.jar'), ('fabric', 'libs/*.jar'), ('player-agent', 'distributions/*.zip')]:
    files = [path for path in (root / module / 'build').glob(pattern) if version in path.name]
    if not files:
        raise SystemExit(f'Missing build artifacts: {module}; run ./gradlew build first')
    for path in files:
        if '-dev' not in path.name:
            shutil.copy2(path, out / path.name)
with zipfile.ZipFile(out / 'minecraft-docs-examples.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    for name in ['README.md', 'STARTUP_GUIDE.md', 'docs', 'examples']:
        path = root / name
        for item in ([path] if path.is_file() else path.rglob('*')):
            if item.is_file(): archive.write(item, item.relative_to(root))
files = sorted(p for p in out.iterdir() if p.name != 'SHA256SUMS')
(out / 'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in files))
print(out)
