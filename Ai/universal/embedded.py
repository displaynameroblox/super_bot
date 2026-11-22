"""Optional SBP gateway mounted on the existing Super-Bot HTTPS server."""
from pathlib import Path
import os
from .host import Host
from .http import create_app, load_config
from .runtime import ExistingProvider, Runtime
from .store import Store


def attach(gateway, config: Path, data_dir: Path):
    principals, limits, existing_provider = load_config(config)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and data_dir.stat().st_mode & 0o077:
        raise ValueError("SBP data directory must be private (chmod 700)")
    store = Store(data_dir / "sbp.sqlite3")
    try:
        host = Host(store, principals, limits)
        runtime = Runtime(host)
        if existing_provider:
            runtime.register_model("existing", ExistingProvider())
        # This is opt-in; existing encrypted chat endpoints and their gateway
        # remain unchanged. SBP has its own bearer credentials and TLS boundary.
        gateway.mount("/", create_app(host, runtime))
    except BaseException:
        store.close()
        raise
    return store
