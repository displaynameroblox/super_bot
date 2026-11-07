# Infrastructure audit — 5 November 2026

## Scope and baseline

Inspected `Ai/server.py`, `local_manager.py`, `preflight.py`, `chat_app.py`,
`tui.py`, the Termux launcher/updater and related tests, TLS/gateway/model
configuration, package metadata, release workflow and installation documentation.
The checkout already contained extensive uncommitted application changes and
an untracked signed Termux updater. Those changes were preserved. This report
covers the observed working tree, not just the last Git commit.

The reported 90% failure rate is a user observation. No installation telemetry
or population-level reproduction was available, so this audit does not establish
that percentage or attribute all failures to one defect.

Focused baseline: 20 tests passed (5 subtests) once loopback sockets were allowed.
The initial restricted full-suite run had 403 passes and 19 failures, all reporting
socket bind permission errors. Those failures were environmental restrictions,
not product bugs. The initial focused run similarly had 18 passes and two
socket-binding failures. No personal LAN host or production credentials were used.

## Confirmed defects and design mismatches

| Finding in the original working tree | Evidence / reproduction | Resolution |
|---|---|---|
| Hardcoded developer LAN source | `start-server.sh` defaulted to a specific `192.168.*` host | Removed default; shared manager is preferred; legacy LAN requires explicit URL |
| Startup depended on provider network availability | `start_server()` called `phone_model.probe()` before starting TLS | Offline transport startup; strict provider probe opt-in; real offline-start regression |
| Launcher silently enabled LAN exposure | Final secure-server invocation forced `HOST=0.0.0.0 SUPER_BOT_ALLOW_LAN=1` | Removed; manager defaults local-only |
| Updater could not import on Windows | Unconditional `fcntl` import | Conditional OS adapters; shared manager uses kernel locks on POSIX/Windows |
| Active-pointer replacement kept a temporary file open | `os.replace` was inside `NamedTemporaryFile` context; Windows sharing semantics differ | Close/fsync before replacement; shared atomic JSON implementation |
| Release publisher and updater spoke different formats | CI published only wheels/sdists; updater requested signed tar archives | One workflow now builds bootstrap packages and signed manifest/archive assets |
| No cross-process lifecycle commands | Manager offered model/library operations, no server stop/status/start | Separate runtime/worker and consistent administration commands |
| Rollback was only an in-process startup fallback | Old updater had an active pointer but no persistent transaction journal or schema contract | Staging, bounded readiness, backup, journal recovery and explicit rollback |
| Startup of source-installed clients ran every development test | `preflight.ensure_tests_pass` invoked unittest discovery by default | Explicit `SUPER_BOT_RUN_TESTS=1` opt-in; `--skip-tests` remains compatible |
| Windows omitted from CI | Test matrix contained Linux/macOS only | Added Windows to Python 3.10/3.13 matrix |
| Dependencies duplicated across package/server lists | `pyproject.toml` and `Ai/requirements.txt` were maintained independently | Package metadata and signed manifest now use the same requirements file |

The original updater already checked signatures, rejected most unsafe tar
members, matched build/startup IDs and could restart a previous release. These
protections were retained rather than replaced by unsigned downloads. The new
path adds manifest compatibility, source trust isolation, Windows path rules,
certificate-pinned readiness and persistent recovery.

## Risks, not measured failures

- Termux native-wheel availability, compilation memory and Android process killing
  can cause installation/runtime failures; actual device testing is still needed.
- The legacy launcher retains platform-specific model tooling and third-party
  native-package bootstrap paths. Shared server management bypasses those paths
  when installed; their supply-chain policy was not redesigned here.
- Broad upstream dependency ranges can change behavior over time. Updates refuse
  unsatisfied dependencies, but exact reproducible dependency environments and
  automatic environment rollback are not implemented.
- A TCP pre-bind probe alone cannot reserve a port. The managed lifetime lock,
  real listener startup and identity-bound health check handle failure; another
  application may still win a port race, which produces a failed start.
- Local file locks assume a local filesystem. Network-filesystem lock and atomic
  rename semantics are outside the supported instance storage contract.
- The release trust root is not provisioned in this checkout. Official signed
  update availability depends on a maintainer establishing the key and publishing
  artifacts. No signing key was generated or release published during this task.

## Implementation phases

A: mapped the existing architecture and ran baseline startup/updater/preflight tests.
B: added private instance state, process-lifetime and operation locks, bounded
readiness/shutdown, offline starts and configuration/identity import.
C: added official/local/LAN sources, signed manifests, bounded cache acquisition,
compatibility checks, safe staging, backups and journaled activation/rollback.
D: added scriptable JSON commands and a separate keyboard dashboard/plain menu.
E: added allowlisted SSH administration with strict host verification; no public
management endpoint or transport-security bypass was introduced.
F: expanded isolated tests and CI, built packaging and documented deployment,
trust setup and recovery. See validation results below.

## Validation

Host: macOS Apple Silicon, Python 3.14. A temporary virtual environment inherited
existing system packages and installed the missing declared dependencies. It was
not a pristine OS install. Global Python was missing `huggingface_hub`; the new
compatibility check correctly refused releases until requirements were installed.

Commands run from the repository root:

```sh
python3 -m pytest Ai/tests/test_termux_updater.py Ai/tests/test_server_startup.py Ai/tests/test_preflight.py -q
python3 -m pytest Ai -q
python3 -m venv --system-site-packages /tmp/superbot-overhaul-env
/tmp/superbot-overhaul-env/bin/python -m pip install -e . pytest build
/tmp/superbot-overhaul-env/bin/python -m pytest Ai/tests/test_manager.py Ai/tests/test_server_startup.py Ai/tests/test_termux_updater.py Ai/tests/test_preflight.py -q
/tmp/superbot-overhaul-env/bin/python -m pytest Ai -q
python3 -m compileall -q Ai/manager_runtime.py Ai/manager_worker.py Ai/manager_cli.py Ai/releases.py Ai/admin.py tools/build_release.py
bash -n Ai/termux/start-server.sh
git diff --check
/tmp/superbot-overhaul-env/bin/python -m build --outdir /tmp/superbot-overhaul-dist
```

The focused implementation run passed 39 tests and 28 subtests. The expanded full
suite passed **448 tests and 198 subtests** in 102.96 seconds. A dependency emitted
one Starlette/httpx deprecation warning. Final targeted checks passed **44 tests and 26 subtests** after cache-read
hardening; a subsequent startup/preflight-only run passed **20 tests and 3 subtests**
after clarifying the opt-in test behavior. One intermediate targeted run failed
because its offline test mocked all file/network reads together; the assertion now
blocks network opener creation while permitting bounded local cache reads.

A first `python -m build --no-isolation` attempt failed because the temporary
environment had no setuptools build backend. The standard isolated build installs
the declared backend; this is a validation environment issue, not a package
runtime failure. `git diff --check` reported existing trailing whitespace on
`.env.example:1`, which was present before this task and was not changed.

Coverage includes real TLS process starts/stops/restarts, occupied ports, duplicate
instances, stale lock release after process death, failed readiness cleanup,
real signed activation and unhealthy-candidate automatic recovery, explicit
rollback, identity/data preservation, trust requirements, official API/channel
resolution via fixtures, real loopback LAN downloads, interrupted transfers,
bad signatures, bad hashes, archive traversal/links/Windows paths, compatibility
and migration refusal, offline cache verification, permission failures,
transaction recovery, initialization, noninteractive admin and SSH argument
restrictions. Existing encrypted transport, chat, account, knowledge and web
regressions passed in the full suite.

Not run here: native Windows/Linux CI jobs, macOS Intel, Android hardware,
interactive curses in different terminals, actual remote SSH hosts, production
GitHub publication, signing-key rotation and power-loss/filesystem-fault injection.
The code targets those OS families; the local test result is not proof of all
platforms. CI runs Windows/Linux/macOS on supported Python versions after push.


The final isolated build produced both wheel and sdist successfully. Archive-content
inspection confirmed manager modules, web assets and dependency metadata were
present and no `.env`, private keys, databases or test modules were included. A
second temporary environment installed the wheel with `--no-deps`; from `/tmp`,
the installed package (not the checkout) passed real offline TLS readiness and
graceful shutdown. Dependencies were inherited, so this is a wheel-isolation smoke
test, not a claim of a pristine-machine installation.

Additional validation commands:

```sh
/tmp/superbot-overhaul-env/bin/python -m pytest Ai/tests/test_manager.py Ai/tests/test_preflight.py Ai/tests/test_server_startup.py -q
python3 -m pytest Ai/tests/test_server_startup.py Ai/tests/test_preflight.py -q
python3 -m venv --system-site-packages /tmp/superbot-wheel-env
/tmp/superbot-wheel-env/bin/python -m pip install --no-deps --force-reinstall /tmp/superbot-overhaul-dist/super_bot-1.0.0-py3-none-any.whl
TERM=xterm /tmp/superbot-overhaul-env/bin/python -m Ai.manager_cli --instance /tmp/superbot-admin-preview admin
```

The last command was exercised in a pseudo-terminal: the dashboard rendered at
80x24, keyboard navigation worked, an attempted start displayed the restricted
socket-binding error, and `q` restored the terminal and exited. This is a limited
local curses smoke test; resize behavior and actual terminal/platform diversity
remain unverified. No server from that preview was left running.
