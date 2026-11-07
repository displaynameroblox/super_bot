# Infrastructure overhaul v1.0

- Added a shared server lifecycle manager with private instance state, cross-platform
  locks, offline start, bounded readiness and graceful shutdown.
- Added configuration/identity import without moving or deleting existing data.
- Added official GitHub, explicitly trusted local/LAN and verified offline-cache sources.
- Added signed compatibility manifests and one dependency source for packaging and releases.
- Added bounded downloads, hardened extraction, transactional activation, server-state
  backups, crash recovery, automatic fallback and explicit rollback.
- Refused unsupported dependency/schema transitions before stopping a working server.
- Added JSON administration commands, log inspection, a separate keyboard dashboard,
  a plain-terminal fallback and strict SSH administration.
- Preserved legacy model/library commands, manual server mode and signed Termux
  updater compatibility; removed hardcoded LAN host and automatic LAN exposure.
- Made model network probes and development tests explicit startup options.
- Added Windows CI, signed-release build tooling and isolated regression tests.
- Documented installation, trust provisioning, migration, architecture, commands,
  recovery and unverified platforms. No releases were published and no production
  signing identity was created.
