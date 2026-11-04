# Service-Scoped Release Activation Implementation Plan

**Approved scope:** The approved server stabilization authorizes bounded service-scoped release activation and brief restarts. This subproject implements the orchestration adapter `scripts/release_activation.py`, a focused native driver `scripts/termux_activation.py`, and comprehensive tests in `tests/test_release_activation.py`. No phone operations, SSH/ADB, live service mutations, package installs, or baseline modifications are permitted in this local task.

**Goal:** Provide deterministic, service-scoped release activation that inspects actual changed file destinations, guards affected services, quiesces running processes with verified death checks, applies the accepted file transaction engine (`scripts/release_transaction.py`), brings services up in strict dependency order, verifies health, commits or rolls back atomically, and supports standalone recovery when candidate bundles are deleted and live imports are broken.

**Base commit:** `284c7dd`. Existing `scripts/release_transaction.py` and `scripts/release-manifest.py` are accepted and pass 198 unit tests.

---

## 1. Architecture and Module Responsibilities

The system cleanly separates file transaction engine concerns from service lifecycle management.

```
       [ Candidate Bundle + Manifest + Preconditions ]
                             |
                             v
               +---------------------------+
               |   release_activation.py   |  <--- Orchestrator (CLI / API)
               +-------------+-------------+
                             |
         +-------------------+-------------------+
         |                                       |
         v                                       v
+------------------------+             +------------------------+
| scripts/               |             | scripts/               |
| release_transaction.py |             | termux_activation.py   |
| (Accepted file engine) |             | (Native Termux Driver) |
+------------------------+             +-----------+------------+
                                                   |
                                                   v
                                       +------------------------+
                                       | scripts/               |
                                       | server_supervisor.py   |
                                       | (Reviewed authority)   |
                                       +------------------------+
```

### Module Responsibilities

1. **`scripts/release_activation.py` (Orchestration Engine & CLI)**:
   - Implements activation workflow: `plan()`, `apply()`, `status()`, `recover()`, `rollback()`.
   - Computes changed destinations from `ReleaseTransaction` prepared state (`action != ACTION_UNTOUCHED`).
   - Maps changed destinations to affected services and runtime components.
   - Enforces lock order: `ActivationLock` -> `services.lock` -> `NamespaceLock`.
   - Manages temporary guard flags with non-secret ownership tokens (`release_guard:{token}`).
   - Manages activation journal transitions (`PREPARED`, `GUARDED`, `QUIESCED`, `APPLIED`, `HEALTHY`, `COMPLETED`, `ROLLING_BACK`, `ROLLED_BACK`, `RECOVERY_REQUIRED`).
   - Copies standalone recovery assets (prior lifecycle scripts, adapter code) into `tx_dir / "recovery_lifecycle"` with SHA-256 hashes.
   - Exposes CLI interface (`plan`, `apply`, `status`, `recover`, `rollback`).

2. **`scripts/termux_activation.py` (Native Termux Lifecycle Driver)**:
   - Defines `LifecycleDriver` abstract protocol / interface.
   - Implements `TermuxLifecycleDriver` binding to `server_supervisor` primitives:
     - `lock(name, blocking, timeout)`
     - `services()`
     - `matching_processes(sid)`
     - `process_identity(pid)`
     - `terminate(records)`
     - `healthy(sid, cfg)`
     - `launch(sid, cfg)`
     - `stop_agy()`
     - `is_paused()`: checks `PAUSED` and `PUBLIC_PAUSED`
     - `find_daemon_process()`: exact argv (`server_supervisor.py --daemon`), UID, starttime
     - `stop_daemon(pid, ident)`: exact termination
     - `start_daemon()`: spawns `python3 server_supervisor.py --daemon` without `--ensure` sweep
   - Provides dynamic isolated loader for preserved prior lifecycle modules during standalone recovery.

3. **Injectable Seam**:
   - `release_activation.py` accepts an optional `driver` parameter defaulting to `TermuxLifecycleDriver()`.
   - In tests, `FakeLifecycleDriver` simulates services, processes, locks, signals, and injected faults without touching workstation or phone processes.

---

## 2. Lock Hierarchy and Serialization

To prevent deadlock across independent locks:

1. **Level 1: `ActivationLock` (`<namespace_root>/.release_activation.lock`)**:
   - Acquired exclusively (`fcntl.LOCK_EX | fcntl.LOCK_NB`) by `release_activation.py` at the start of any mutating command (`apply`, `rollback`, `recover`).
   - Held throughout the entire activation/recovery lifecycle.
   - Prevents concurrent activation operations on the same namespace.

2. **Level 2: `services.lock` (`<STATE>/services.lock`)**:
   - Acquired exclusively before inspecting or altering service flags (`DISABLED`).
   - Acquired during process quiescence and service health/launch.
   - **Crucial Rule**: Never hold `services.lock` when waiting for supervisor daemon termination, because a running daemon in `cycle()` may block waiting for `services.lock`. Guard flags and service quiescence are completed first, `services.lock` is released, daemon is stopped, and `services.lock` is reacquired and rechecked before file writes.

3. **Level 3: `NamespaceLock` (`<namespace_root>/.release_transaction.lock`)**:
   - Acquired internally by `ReleaseTransaction` methods (`prepare`, `apply`, `commit`, `rollback`, `recover`).
   - Never acquired by `release_activation.py` directly; no nested or duplicate flock on this file.

Strict ordering: `ActivationLock` -> `services.lock` -> `NamespaceLock`. Lock inversion is structurally impossible.

---

## 3. Destination-to-Service Mapping

Only ACTUAL changed destinations (`action != ACTION_UNTOUCHED`) affect services:

| Destination Pattern / Path | Affected Component / Service |
|----------------------------|------------------------------|
| `usr/bin/dashboard`<br>`home/.config/dashboard/static/*`<br>`home/.local/lib/mobile-server/*` (except `service_catalog.py`) | `dashboard` |
| `home/bin/mobile-gateway` | `transfer_gateway` |
| `home/.config/caddy/Caddyfile` | `caddy` |
| `home/bin/server_supervisor.py`<br>`home/.local/lib/mobile-server/service_catalog.py` | `supervisor` (daemon) + `dashboard` (if catalog changed) |
| `home/bin/termux_session.sh`<br>`home/.config/ttyd/termux_index.html` | `terminal` |
| `home/bin/laptop_session.py` | `laptop_terminal` |
| `home/bin/agy_session.sh`<br>`home/.config/ttyd/agy_index.html`<br>`usr/bin/agy-resume`<br>`usr/var/lib/proot-distro/.../agy-runner-loop` | `agy` |
| `home/bin/battery_guard.sh` | `battery_guard` |
| `home/.config/tor/torrc` | `tor` |
| `scripts/server-user`<br>`scripts/server-status`<br>`scripts/server-health`<br>`scripts/server-up`<br>`scripts/server-down`<br>`scripts/server-restart`<br>`home/start_services.sh` | CLI tools / boot entry (no running daemon) |

### Strict Safety Invariants
- **Unexplained Destinations**: Any changed destination outside known mappings causes fail-closed abort.
- **Suwayomi Immutability**: Any changed file in Suwayomi scripts (`start_suwayomi.sh`, `suwayomi_daily_update.py`, `suwayomi_graphql_filter.py`, `suwayomi_update.sh`) is immediately rejected fail-closed.
- **SSH Immunity**: `ssh` is never an affected service. Explicit check raises error if `ssh` is selected.
- **Preserve Disabled**: Any service with a pre-existing flag in `DISABLED / sid` is never started or restarted, even if its files changed.
- **No Unrelated Restarts**: Unaffected running services continue running without interruption.

---

## 4. Flag Preservation & Temporary Guard Protocol

The supervisor treats files in `DISABLED` as presence markers.

1. **Pre-Existing Flags**:
   - If `DISABLED / sid` exists before activation:
     - Record metadata: `ino`, `dev`, `mode`, `uid`, `size`, `sha256`.
     - Mark `pre_existing = True` in activation journal.
     - **Inviolable Rule**: Do not touch, truncate, rewrite, chmod, or unlink pre-existing flags.

2. **Temporary Guard Creation**:
   - For originally absent flags (`pre_existing = False`):
     - Generate a unique non-secret token: `f"rel_guard_{uuid4().hex}"`.
     - Record intent in activation journal before writing to disk.
     - Write token string to `DISABLED / sid` with mode `0600`, fsync, close.
     - Record resulting `ino`, `dev`, `mode`, `uid`, `token` in journal.

3. **Guard Removal**:
   - Under `services.lock`:
     - Inspect `DISABLED / sid`.
     - Verify: file is a regular file, owned by runtime UID, mode `0600`, content matches recorded token, inode matches recorded inode (or recorded token matches on recovery crash-before-inode).
     - Unlink guard.
     - If concurrent edit, wrong owner, symlink, or modified content: abort fail-closed with `ConcurrencyError`; do not unlink.

---

## 5. Quiescence, Execution, and Health Verification

### Step-by-Step Activation Sequence

1. **Preparation**:
   - Acquire `ActivationLock`.
   - Verify server is not paused (`is_paused() == False`).
   - Call `release_transaction.prepare(bundle, manifest, preconds, tx_dir, ns_root)`.
   - Snapshot prior lifecycle scripts into `tx_dir / "recovery_lifecycle"`.
   - Compute affected services from changed destinations.
   - If no affected services, proceed to file apply directly without service disruption.

2. **Guarding & Quiescence**:
   - Under `services.lock`:
     - Record original active/disabled state for all affected services.
     - Install temporary guards for originally active affected services.
     - Record running process identities (`matching_processes(sid)`).
     - Call `terminate(records)`.
     - **Explicit Termination Check**: verify `process_identity(pid)` and `matching_processes(sid)` return empty. If any process survives: abort activation, keep guards in place, do not mutate files!
   - If supervisor/catalog changed:
     - Check if daemon is running.
     - Release `services.lock`.
     - Stop daemon by exact argv, UID, starttime. Verify exit.
     - Reacquire `services.lock`, re-verify guards and dead processes.

3. **File Replacement**:
   - Under `services.lock`:
     - Call `tx.apply(namespace_root=ns_root)`.
     - Files are now atomically replaced.

4. **Service Reactivation & Dependency Probing**:
   - Under `services.lock`:
     - Start only originally active affected services in dependency order:
       - `filebrowser` (if affected and active) -> probe healthy.
       - `transfer_gateway` (if affected and active; requires filebrowser healthy) -> probe healthy.
       - `dashboard` (if affected and active) -> probe healthy.
       - `caddy` (if affected and active; requires dashboard/gateway healthy) -> probe healthy.
       - other affected services -> probe healthy.
     - If supervisor changed and daemon was originally running:
       - Restart daemon (`start_daemon()`) without `--ensure` sweep.
       - Verify daemon started and holds `daemon.lock`.

5. **Finalization**:
   - Under `services.lock`:
     - Re-verify health of all affected services.
     - Remove temporary guards after identity validation.
     - Release `services.lock`.
   - Call `tx.commit(namespace_root=ns_root)`.
   - Update activation journal to `STATE_COMPLETED`.
   - Release `ActivationLock`.

---

## 6. Failure Rollback & Standalone Recovery

### Rollback on Failure
If any failure occurs during file apply, service launch, or health probing:
1. Ensure guards are present for all affected services.
2. Quiesce any candidate processes that were launched (`terminate(candidate_records)`), verify complete exit.
3. Call `tx.rollback(namespace_root=ns_root)`: restores original file backups, returns created files to absent.
4. Relaunch original services using prior lifecycle authority, verify health.
5. Restore original flags (unlink temporary guards).
6. If daemon was originally running, restart daemon.
7. Record `STATE_ROLLED_BACK` in activation journal.

### Standalone Recovery (`release_activation.py recover`)
When invoked after an interrupted/crashed activation:
1. Acquire `ActivationLock`.
2. Load activation journal from `tx_dir / "activation_journal.json"`.
3. If already `STATE_COMPLETED`: report success, exit.
4. If in-progress (`GUARDED`, `QUIESCED`, `APPLIED`, `HEALTHY`):
   - Load preserved lifecycle authority from `tx_dir / "recovery_lifecycle"`.
   - Under `services.lock`:
     - Quiesce any running candidate processes.
     - Call `tx.rollback(namespace_root=ns_root)`.
     - Relaunch original active services.
     - Validate and remove owned guards using token checks.
     - Restart daemon if it was originally running.
   - Record `STATE_ROLLED_BACK`.

---

## 7. Decisive Test Scenarios

`tests/test_release_activation.py` will prove:
1. **Restart Mapping**: Only affected services are selected based on actual changed destinations.
2. **No-Op Release**: Unchanged files result in zero service stops/restarts.
3. **Disabled Services Untouched**: Pre-disabled services (e.g. Syncthing, Vaultwarden, terminal, Tor) are never started; flag bytes/inodes are untouched.
4. **SSH & Unrelated Isolation**: SSH is never selected; unrelated running services continue uninterrupted.
5. **Surviving Process Guard**: If a process survives termination, file mutation is blocked, guards remain in place.
6. **Guard Crash Before Inode Journal**: Guard with ownership token is recognized and cleaned up during recovery even if crash happened before inode was saved.
7. **Concurrent Guard Edit Refusal**: Concurrent alteration to guard file aborts fail-closed without unlinking.
8. **Interruption During File Apply**: Injected apply failure triggers candidate quiescence and complete preimage restoration.
9. **Candidate Health Failure**: Unhealthy candidate triggers candidate process quiescence, file rollback, and original service restoration.
10. **Crash After Candidate Start**: Candidate processes running during interrupted activation are quiesced before file rollback.
11. **Prior Daemon Intent**: Daemon is restarted only if originally running; `--ensure` sweep is not invoked.
12. **Standalone Recovery with Corrupted Imports**: Standalone recovery recovers successfully with bundle deleted and live dashboard/catalog corrupted.
13. **Strict Lock Ordering**: No nested flock deadlock between `ActivationLock`, `services.lock`, and `NamespaceLock`.
14. **Idempotence**: Repeated apply/recover calls on committed or rolled-back state verify real disk state without spurious errors.
