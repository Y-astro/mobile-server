# Server Operations Reference

Authoritative technical reference for managed services, runtime lifecycle contracts, durable data paths, health inspection, and backup boundaries. Based on maintained repository sources and sanitized audit findings from 2026-11-03.

---

## 1. Service Ownership and Endpoint Reference

The server manages 15 background services via [`scripts/server_supervisor.py`](../scripts/server_supervisor.py#L37-L72) and [`app/service_catalog.py`](../app/service_catalog.py#L12-L73), plus on-demand utilities and external laptop integrations.

### Managed Supervisor Services (15 Services)

| Service ID (`sid`) | Category & Purpose | Maintained Source / Owner | Runtime Launcher & Executable | Endpoint & Address Binding | Health Inspection Criteria | Durable Data & Config Paths |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `dashboard` | Core web portal & role authorization | [`app/dashboard.py`](../app/dashboard.py), helpers in [`app/`](../app/) | `$PREFIX/bin/python3 -u $PREFIX/bin/dashboard` | `127.0.0.1:8001` (HTTP, pinned loopback in source) | `GET /healthz` (supervisor accepts 200, redirects, 401/403) | Creds: `$HOME/.config/dashboard/users.json` (0600)<br>Notes: `$HOME/.config/dashboard/notes.json`<br>Static: `$HOME/.config/dashboard/static/` |
| `caddy` | Ingress reverse proxy & TLS termination | [`config/Caddyfile`](../config/Caddyfile) | `$PREFIX/bin/caddy run --config $HOME/.config/caddy/Caddyfile` | Ports `:8000`, `:8443`, `:8444`, `:4567` (bound per Caddyfile) | `GET /healthz` (proxies to :8001; supervisor accepts 200, redirects, 401/403, 502–504) | Config: `$HOME/.config/caddy/Caddyfile`<br>TLS/CA keys: `$HOME/.local/share/caddy/` |
| `filebrowser` | Cloud storage & media browser | Native FileBrowser binary ([licenses/filebrowser.txt](../licenses/filebrowser.txt)) | `$PREFIX/bin/filebrowser -d $HOME/.config/filebrowser/filebrowser.db -a 127.0.0.1 -p 8085 -c $HOME/.config/filebrowser/fb_config.json --cacheDir $HOME/.cache/filebrowser --disableImageResolutionCalc --imageProcessors 2` | `127.0.0.1:8085` (HTTP, pinned loopback via `-a`) | `GET /drive/` (supervisor accepts 200, redirects, 401/403) | DB: FileBrowser application database `$HOME/.config/filebrowser/filebrowser.db` (format unverified)<br>Config: `$HOME/.config/filebrowser/fb_config.json`<br>Cache: `$HOME/.cache/filebrowser`<br>Media: `/storage/emulated/0/` |
| `transfer_gateway` | Bounded streaming admission proxy | [`gateway/`](../gateway/) (Go source) | `$HOME/bin/mobile-gateway -backend http://127.0.0.1:8085` | `127.0.0.1:8081` (HTTP, loopback default in main.go) | `GET /healthz` (supervisor accepts 200, redirects, 401/403) | Stateless admission proxy; no durable DB |
| `suwayomi` | Manga server backend | [`scripts/start_suwayomi.sh`](../scripts/start_suwayomi.sh) | `$HOME/bin/start_suwayomi.sh` (executes `Suwayomi-Server.jar` via OpenJDK 21 or Debian PRoot fallback) | Port `:4569` (HTTP backend, loopback); client port `:4567` | Supervisor: `GET /suwayomi/` on :4569 (accepts 200, redirects, 401/403); dashboard reachability may fall back to :4567 | Data dir: `$HOME/suwayomi/data/`<br>DB: Suwayomi database files in `$HOME/suwayomi/data/` (format unverified)<br>Downloads: `$HOME/suwayomi/data/downloads/`<br>Logs: `$HOME/suwayomi/data/logs/` |
| `suwayomi_filter` | Suwayomi GraphQL reading-feed filter | [`scripts/suwayomi_graphql_filter.py`](../scripts/suwayomi_graphql_filter.py) | `$PREFIX/bin/python3 $HOME/bin/suwayomi_graphql_filter.py` | `127.0.0.1:4566` (HTTP, pinned loopback in source) | `GET /healthz` (supervisor accepts 200, redirects, 401/403) | Logs: `$HOME/suwayomi/data/logs/graphql_filter.log` |
| `ssh` | Remote administrative shell | Termux OpenSSH package | `$PREFIX/bin/sshd -D` | Port `:8022` (TCP, OpenSSH listener) | TCP probe on 8022 (banner `SSH-`) | Config: `$PREFIX/etc/ssh/`<br>Keys: `$HOME/.ssh/authorized_keys` |
| `agy` | Autonomous AI coding agent | [`scripts/agy_session.sh`](../scripts/agy_session.sh), [`scripts/agy-runner-loop`](../scripts/agy-runner-loop), [`web/agy_index.html`](../web/agy_index.html), [`app/agy_sessions.py`](../app/agy_sessions.py) | `$PREFIX/bin/ttyd -W -a -m 8 -i 127.0.0.1 -p 7682 -b /agy -I $HOME/.config/ttyd/agy_index.html $HOME/bin/agy_session.sh` | `127.0.0.1:7682` (HTTP/WS, pinned loopback via `-i`) | `GET /agy/` (supervisor accepts 200, redirects, 401/403) | Config: `$HOME/.config/ttyd/agy_index.html`<br>tmux socket: `$PREFIX/var/run/tmux-10505/default`<br>Lock: `$HOME/.config/server_recovery/agy*.lock`<br>History: PRoot Debian `/root/.local/state/` |
| `laptop_terminal` | Restricted laptop terminal bridge | [`app/laptop.py`](../app/laptop.py), [`scripts/laptop_session.py`](../scripts/laptop_session.py), [`web/laptop.js`](../web/laptop.js), [`laptop/bridge.py`](../laptop/bridge.py) | `$PREFIX/bin/ttyd -W -a -m 8 -i 127.0.0.1 -p 7683 -b /laptop $PREFIX/bin/python3 $HOME/bin/laptop_session.py` | `127.0.0.1:7683` (HTTP/WS, pinned loopback via `-i`) | `GET /laptop/` (supervisor accepts 200, redirects, 401/403) | Pairing: `$HOME/.config/mobile-server-laptop/client.json`, `id_ed25519` (0600), `known_hosts` (0600) |
| `adguard` | Network DNS & ad-blocking | AdGuardHome binary | `$HOME/adguard/AdGuardHome/AdGuardHome -c $HOME/adguard/data/AdGuardHome.yaml -w $HOME/adguard/data --no-permcheck --no-etc-hosts` | Ports `:3000` (HTTP console), `:5353` (DNS), bound per config | `GET /` (supervisor accepts 200, redirects, 401/403) | Config: `$HOME/adguard/data/AdGuardHome.yaml`<br>Data & query logs: `$HOME/adguard/data/` |
| `battery_guard` | Battery & thermal monitor | [`scripts/battery_guard.sh`](../scripts/battery_guard.sh) | `$PREFIX/bin/bash $HOME/bin/battery_guard.sh --daemon` | None (daemon process) | Process match + `$HOME/.config/battery_guard/status.json` mtime < 360s | State: `$HOME/.config/battery_guard/status.json`<br>Logs: `$HOME/.config/battery_guard/` |
| `syncthing` *(disabled)* | Continuous file synchronization | Termux Syncthing package | `$PREFIX/bin/syncthing serve --config $HOME/.local/state/syncthing --data $HOME/.local/state/syncthing --allow-newer-config --no-browser` | Port `:8384` (HTTP, bound per config.xml) | `GET /rest/noauth/health` (supervisor accepts 200, redirects, 401/403) | State & DB: `$HOME/.local/state/syncthing/` (`config.xml`, `index-*.db`, `cert.pem`, `key.pem`) |
| `vaultwarden` *(disabled)* | Bitwarden password vault | Vaultwarden binary in Debian PRoot | `$PREFIX/bin/proot-distro login debian -- /usr/bin/env ROCKET_PORT=8082 ROCKET_ADDRESS=127.0.0.1 DATA_FOLDER=$HOME/vaultwarden/data WEB_VAULT_FOLDER=$HOME/vaultwarden/web-vault $HOME/vaultwarden/vaultwarden` | `127.0.0.1:8082` (HTTP, loopback passed via ROCKET_ADDRESS) | `GET /alive` (supervisor accepts 200, redirects, 401/403) | Vault data: `$HOME/vaultwarden/data/` (`db.sqlite3`, RSA keys)<br>Web UI: `$HOME/vaultwarden/web-vault/` |
| `terminal` *(disabled)* | Admin Termux web shell | `ttyd` pkg + [`scripts/termux_session.sh`](../scripts/termux_session.sh), [`web/termux_index.html`](../web/termux_index.html) | `$PREFIX/bin/ttyd -W -i 127.0.0.1 -p 7681 -b /terminal -I $HOME/.config/ttyd/termux_index.html $HOME/bin/termux_session.sh` | `127.0.0.1:7681` (HTTP/WS, pinned loopback via `-i`) | `GET /terminal/` (supervisor accepts 200, redirects, 401/403) | Config: `$HOME/.config/ttyd/termux_index.html` |
| `tor` *(disabled)* | Tor SOCKS proxy | Termux Tor pkg + [`config/torrc`](../config/torrc) | `$PREFIX/bin/tor -f $HOME/.config/tor/torrc` | `127.0.0.1:9050` (SOCKS5, pinned in config/torrc) | SOCKS5 handshake (`\x05\x01\x00` -> `\x05\x00`) | Config: [`config/torrc`](../config/torrc)<br>Data: `$HOME/.local/share/mobile-server-tor` |

### On-Demand Utility

- **`converter`**: [`app/document_conversion.py`](../app/document_conversion.py). Invokes headless LibreOffice in Debian PRoot (`proot-distro login debian -- libreoffice -env:UserInstallation=file://.../profile --headless --convert-to pdf ...`). Bounded process execution; consumes 0 MB RAM idle footprint; writes temporary scratch batches to `$HOME/data/conversions/batch_<id>/` (configurable via `conversion_root`).

### External Laptop Integration (Wealthfolio)

- **Ingress & Routing**: Caddy exposes port `:8444` ([`config/Caddyfile:217-255`](../config/Caddyfile#L217-L255)) and reverse-proxies `/wealth/*` to loopback `127.0.0.1:18930`.
- **Limited Laptop Ownership Evidence**: Maintained configuration proves a laptop-backed reverse proxy to loopback `127.0.0.1:18930` (laptop SSH tunnel) with an offline HTTP 503 fallback page, and confirms no phone-side Wealthfolio database or service is configured in maintained code. (This proves the absence of a configured phone-side service/database, not a universal statement about unmanaged device storage).

### Port and Path Disambiguation

- **Storage & Web Ingress**:
  - `8085` (`filebrowser`): Native loopback backend.
  - `8081` (`transfer_gateway`): Bounded Go streaming proxy on loopback; enforces connection/TUS chunk limits, proxying to `8085`.
  - `8001` (`dashboard`): Python application on loopback; provides role authorization, API endpoints, notes, and telemetry.
  - `8000` (HTTP) / `8443` (HTTPS): Public Caddy edge listeners; require `forward_auth` against `:8001` before proxying to `:8081` (`/drive/*`) or `:8001`.
- **Suwayomi Readers & Filter**:
  - `4569`: Internal Suwayomi backend port (loopback).
  - `4566`: Suwayomi GraphQL filter proxy (loopback); scopes updates to category 1 and skips read chapters.
  - `4567`: Native reader port. Intentionally runs **without** portal `forward_auth` for Mihon/Tachiyomi sync.
  - Portal route `/suwayomi/*` on `:8000`/`:8443`: Requires admin `forward_auth` before proxying to backend `:4569`.

---

## 2. Code vs. Durable-Data Separation

### Deployable Code and Managed Configuration

Tracked repository assets and deployment mapping:
- **Source trees**: `app/`, `config/`, `gateway/`, `laptop/`, `scripts/`, `web/`.
- **Audited baseline vs candidate manifests**: Historical [`config/deployment-files.json`](../config/deployment-files.json) is a frozen 35-row baseline capturing original audited installed files. Current candidate manifests are generated via [`scripts/release-manifest.py`](../scripts/release-manifest.py). The generator discovers flat runtime helpers in `app/` (excluding `dashboard.py`), includes explicitly mapped web/ttyd assets, and optionally includes the compiled Go gateway (`.work/mobile-gateway`). A complete deployment requires the gateway. New web assets require explicit reviewed mappings.
- **Runtime helpers**: Installed to `$HOME/.local/lib/mobile-server/`:
  - `agy_sessions.py` (tmux session manager)
  - `auth.py` (credential validation and store)
  - `document_conversion.py` (LibreOffice PDF conversion)
  - `laptop.py` (laptop SSH bridge client)
  - `limits.py` (defines `Admission` semaphore controller and `BODY_LIMITS`)
  - `notes.py` (quick notes store)
  - `role_templates.py` (role template loader)
  - `service_catalog.py` (shared service identities and endpoints)
  - `service_panel.py` (status aggregator and lifecycle controls)
  - `telemetry.py` (hardware and resource telemetry)
  - `tor_proxy.py` (Tor SOCKS client)
- **Entrypoints**: `$PREFIX/bin/dashboard`, `$HOME/bin/server_supervisor.py`, `$HOME/bin/mobile-gateway`, and `$PREFIX/bin/server-*` wrappers.

### Excluded Sensitive Data and Runtime State

Sensitive credentials, databases, keys, user content, and session records are excluded from source tracking and release manifests (paths listed, contents omitted):
- **Credentials**: `$HOME/.config/dashboard/users.json` (mode 0600 PBKDF2 password records).
- **Session Keys**: `$HOME/.config/dashboard/` session signing keys.
- **SSH Keys & Pairing**: `$HOME/.config/mobile-server-laptop/id_ed25519` (0600), `client.json` (0600), `known_hosts` (0600); host keys in `$PREFIX/etc/ssh/ssh_host_*_key`.
- **TLS Certificates & CA Keys**: `$HOME/.local/share/caddy/pki/` (Caddy internal root/intermediate CA keys).
- **Databases**:
  - FileBrowser: `$HOME/.config/filebrowser/filebrowser.db` (application database, format unverified).
  - Suwayomi: Suwayomi database files in `$HOME/suwayomi/data/` (format unverified).
  - Vaultwarden: `$HOME/vaultwarden/data/db.sqlite3` and RSA signing keys.
  - AdGuard Home: `$HOME/adguard/data/` (runtime YAML with credential hashes, query logs).
  - Syncthing: `$HOME/.local/state/syncthing/` (`config.xml`, `index-*.db`, `cert.pem`, `key.pem`).
- **User Content & Media**: `/storage/emulated/0/` (Android shared storage), `$HOME/suwayomi/data/downloads/`, `$HOME/.config/dashboard/notes.json`.
- **AGY Session Logs & History**: `$HOME/.local/state/agy/`, PRoot Debian `/root/.local/state/`, active tmux socket at `$PREFIX/var/run/tmux-10505/default`.

### Namespace, UID, and Environment Conventions

- Standard paths: `$HOME` = `/data/data/com.termux/files/home`; `$PREFIX` = `/data/data/com.termux/files/usr`.
- Assumed identity: User `u0_a505` (UID `10505`).
- **Migration Boundary**: Android assigns package UIDs dynamically. An alternate UID or directory path breaks hardcoded paths and isolated socket directories (`tmux-10505`). Transferring data or software to an alternate UID or environment requires an explicit, reviewed migration rather than blind restoration.

---

## 3. Supervisor Authority and Lifecycle Contracts

The 15 managed services listed above use [`scripts/server_supervisor.py`](../scripts/server_supervisor.py). This does not establish ownership of every unmanaged process on the phone.

### Supervisor Locks

1. **`services.lock`** (`$HOME/.config/server_recovery/services.lock`): Acquired with `fcntl.flock` during health cycles, CLI commands, and pause/resume transitions ([`scripts/server_supervisor.py:63-78`](../scripts/server_supervisor.py#L63-L78)). Serializes supervisor cycles and lifecycle modifications.
2. **`daemon.lock`** (`$HOME/.config/server_recovery/daemon.lock`): Held non-blocking exclusively by the running supervisor daemon ([`scripts/server_supervisor.py:389-410`](../scripts/server_supervisor.py#L389-L410)). Guarantees a singleton supervisor daemon.

### Exact Process Matching

- **Command-Line Matching**: [`matching_processes(sid)`](../scripts/server_supervisor.py#L96-L139) parses `/proc`, checks `proc.stat().st_uid == os.getuid()`, and matches explicit argv tokens. Broad `pkill` patterns and the invoking SSH session are never matched.
- **Starttime Identity**: [`process_identity(pid)`](../scripts/server_supervisor.py#L86-L93) reads `/proc/{pid}/stat`, skips zombies (`Z`), and captures `stat[19]` (field 22, starttime). Process termination rechecks this identity to reduce PID-reuse risk. HTTP and socket health probes check endpoint responses rather than process starttime.

### Process Termination Reality

[`terminate(records)`](../scripts/server_supervisor.py#L142-L153) issues `SIGTERM` (4-second wait), then `SIGKILL` (1-second wait).
> [!IMPORTANT]
> `terminate()` returns implicitly once its deadline expires, even if matching processes survive. The release adapter must explicitly recheck that matching identities are absent before replacing files or declaring quiescence.

### Per-Service Disable Markers

- Presence markers stored at `$HOME/.config/services_disabled/<sid>`.
- The supervisor cycle inspects flag existence ([`scripts/server_supervisor.py:266`](../scripts/server_supervisor.py#L266)); flagged services are skipped and marked `disabled`.
- Touching disables; unlinking enables. Touching an existing marker preserves its contents and permissions but changes timestamps. Release activation must preserve pre-existing markers without touching them.

### Global Pause Semantics

- Invoked via `server_supervisor.py --pause` (or `server-down`).
- Creates markers `$HOME/.config/server_recovery/paused` and `/storage/emulated/0/.phone-server-paused`.
- Terminates all managed services **except SSH** (port 8022 stays active for recovery).
- Calls [`stop_agy()`](../scripts/server_supervisor.py#L155-L189) to terminate AGY runner loops, PRoot descendants, and tmux panes.
- Resumed with `server_supervisor.py --resume` (or `server-up`), which removes markers and calls `ensure()`.

### Activation Adapter Status and Installer Guard

> [!WARNING]
> The transactional release activation adapter is currently being implemented and reviewed in a separate branch (`release-activation`); it is **not available** in maintained HEAD.
> The legacy installer ([`scripts/install.sh:8-11`](../scripts/install.sh#L8-L11)) terminates immediately at an explicit fail-fast guard (`exit 1`) at entry, before running baseline checks or tool verification. Direct source writes via the legacy installer are disabled until the reviewed transactional activation adapter takes over. File-only recovery requires stopping affected processes and ensuring service quiescence first.

---

## 4. Preserving Intentional Disabled Services & Known Limits

### Dated Observed Intent (2026-11-03 Snapshot)

During the 2026-11-03 server audit, four services had active markers in `$HOME/.config/services_disabled/`:
1. `syncthing`
2. `vaultwarden`
3. `terminal`
4. `tor`

This snapshot records observed intent as of that date, not a permanent rule to overwrite subsequent user changes. **Do not force-start disabled services during investigations.**

### Known Limits and Unverified Configurations

- **Syncthing**: Configuration exists at `$HOME/.local/state/syncthing/config.xml` with one folder target. Live folder synchronization has not been verified.
- **Vaultwarden**: Host directories `$HOME/vaultwarden/data` and `$HOME/vaultwarden/web-vault` exist. PRoot path translation and Web Vault UI compatibility under Debian PRoot remain unverified.
- **PRoot Debian Container Layout**: Active Debian rootfs is located at `$PREFIX/var/lib/proot-distro/containers/debian/rootfs` (the observed modern layout, not legacy installed-rootfs paths).

---

## 5. Read-Only Health and Status Inspection

### Supervisor Status CLI vs. Read-Only Probes

- **Supervisor Status CLI**: Running `python3 $HOME/bin/server_supervisor.py --status` (or wrappers `server-health` / `server-status`) inspects supervisor state and reports service health. Note that while it does not stop, start, or signal services, **it is not strictly a zero-filesystem-write command**: its entrypoint invokes [`initialize()`](../scripts/server_supervisor.py#L51-L60), which creates missing directories (`.config/server_recovery`, `.config/services_disabled`, `.cache/filebrowser`, `.config/battery_guard`) and initializes a rotating file log handler on disk.
- **Read-Only Network Probes**: Direct health requests and socket checks do not request configuration or lifecycle changes. Serving these requests may still write normal access logs or update caches; zero filesystem writes are not guaranteed.

Supervisor [`healthy(sid, cfg)`](../scripts/server_supervisor.py#L191-L217) accepts HTTP status `200, 204, 301, 302, 303, 307, 308, 401, 403` (and for `caddy`, additionally `502, 503, 504` during backend recovery).

*(Audited baseline on 2026-11-03 recorded 11 healthy services and 4 intentional disabled services; do not infer current real-time uptime from static audit logs).*

### Read-Only Inspection Table (No Credentials Required)

| Target Service | Protocol / Command | Healthy Inspection Rule |
| :--- | :--- | :--- |
| `caddy` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/healthz` | Accepts 200, redirects, 401/403, 502–504 |
| `dashboard` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/healthz` | Accepts 200, redirects, 401/403 |
| `filebrowser` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8085/drive/` | Accepts 200, redirects, 401/403 |
| `transfer_gateway`| `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/healthz` | Accepts 200, redirects, 401/403 |
| `suwayomi` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4569/suwayomi/` | Accepts 200, redirects, 401/403 |
| `suwayomi_filter` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4566/healthz` | Accepts 200, redirects, 401/403 |
| `adguard` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3000/` | Accepts 200, redirects, 401/403 |
| `ssh` | TCP probe on `127.0.0.1:8022` | Banner starts with `SSH-` |
| `tor` *(if enabled)* | Send SOCKS5 greeting `\x05\x01\x00` to `127.0.0.1:9050` | Response matches `\x05\x00` |
| `syncthing` *(if enabled)* | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8384/rest/noauth/health` | Accepts 200, redirects, 401/403 |
| `vaultwarden` *(if enabled)* | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8082/alive` | Accepts 200, redirects, 401/403 |
| `battery_guard` | Check matching daemon process and `$HOME/.config/battery_guard/status.json` | Matching process exists and status file was modified within 360s |

---

## 6. Backup and Restore Requirements

### Software Rollback vs. Data Restore

- **Software Rollback**: Restores the verified set of deployable code, scripts, static web templates, and configuration files via release manifests and file journals. Software rollback **does not restore databases, user files, credentials, or session states**.
- **Data Restore**: Replaces stateful database files and user records. Must be handled as an independent operation with distinct validation.

> [!CAUTION]
> **No Live Database Copying**: Copying running database files (`filebrowser.db`, Suwayomi database files, Vaultwarden `db.sqlite3`) while services are active risks torn writes, corrupt journal states, and data loss.
> Configuration exports (such as `AdGuardHome.yaml` or `config.xml`) contain settings metadata, **not** database records or user content.

### Verified vs. Unverified Backup Boundaries

- **Verified Mechanisms**:
  - The repository release engine supports verifying and snapshotting **deployable software assets and static configurations only**.
  - **No automated online database backup mechanisms currently exist** in local repository sources.
- **Unverified Procedures**:
  - A supported consistent export or a separately reviewed stopped-service procedure must be established before attempting database backups, preserving original disabled intent and avoiding nested supervisor locks (such as acquiring `services.lock` in a caller before invoking supervisor CLI, which attempts to acquire the same lock and deadlocks).
  - No backup operation is authorized by this documentation task, and no generic file-copying recipe is established.
- **Deferred Investigations**: Deep Suwayomi backup schedules, download cache management, and Tachiyomi cloud sync ownership are deferred to subsequent investigation phases.
