# Server Operations Reference

Authoritative technical reference for managed services, runtime lifecycle contracts, durable data paths, health inspection, and backup boundaries. Based on maintained sources and 2026-11-03 audit findings.

---

## 1. Service Ownership and Endpoint Reference

The server manages 15 background services via [`scripts/server_supervisor.py`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L37-L73) and [`app/service_catalog.py`](file:///home/astro/server-audit/2026-11-03/server-operations/app/service_catalog.py#L12-L73), plus on-demand utilities and laptop integrations.

### Managed Supervisor Services (15 Services)

| Service ID (`sid`) | Category & Purpose | Maintained Source / Owner | Runtime Launcher & Executable | Endpoint & Protocol | Health Probe (Read-Only) | Durable Data & Config Paths |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `dashboard` | Core web portal & role authorization | [`app/dashboard.py`](file:///home/astro/server-audit/2026-11-03/server-operations/app/dashboard.py), [`app/`](file:///home/astro/server-audit/2026-11-03/server-operations/app/) | `$PREFIX/bin/python3 -u $PREFIX/bin/dashboard` | `127.0.0.1:8001` (HTTP) | `GET /healthz` (200) | Creds: `$HOME/.config/dashboard/users.json` (0600)<br>Notes: `$HOME/.config/dashboard/notes.json`<br>Static: `$HOME/.config/dashboard/static/` |
| `caddy` | Ingress reverse proxy & TLS termination | [`config/Caddyfile`](file:///home/astro/server-audit/2026-11-03/server-operations/config/Caddyfile) | `$PREFIX/bin/caddy run --config $HOME/.config/caddy/Caddyfile` | `0.0.0.0:8000` (HTTP)<br>`0.0.0.0:8443` (HTTPS) | `GET /healthz` (proxies to :8001) | Config: `$HOME/.config/caddy/Caddyfile`<br>TLS/CA keys: `$HOME/.local/share/caddy/` |
| `filebrowser` | Cloud storage & media browser | Native FileBrowser v2.63.23 ([licenses/filebrowser.txt](file:///home/astro/server-audit/2026-11-03/server-operations/licenses/filebrowser.txt)) | `$PREFIX/bin/filebrowser -d $HOME/.config/filebrowser/filebrowser.db -a 127.0.0.1 -p 8085 -c $HOME/.config/filebrowser/fb_config.json --cacheDir $HOME/.cache/filebrowser --disableImageResolutionCalc --imageProcessors 2` | `127.0.0.1:8085` (HTTP, loopback) | `GET /drive/` (200) | DB: `$HOME/.config/filebrowser/filebrowser.db`<br>Config: `$HOME/.config/filebrowser/fb_config.json`<br>Cache: `$HOME/.cache/filebrowser`<br>Media: `/storage/emulated/0/` |
| `transfer_gateway` | Bounded streaming admission proxy | [`gateway/`](file:///home/astro/server-audit/2026-11-03/server-operations/gateway/) (Go source) | `$HOME/bin/mobile-gateway -backend http://127.0.0.1:8085` | `127.0.0.1:8081` (HTTP, loopback) | `GET /healthz` (200) | Stateless admission proxy; no durable DB |
| `suwayomi` | Manga server backend | [`scripts/start_suwayomi.sh`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/start_suwayomi.sh) | `$HOME/bin/start_suwayomi.sh` (`Suwayomi-Server.jar` via OpenJDK 21 or Debian PRoot fallback) | `127.0.0.1:4569` (HTTP backend)<br>`0.0.0.0:4567` (HTTP client reader) | `GET /suwayomi/` on :4569 or :4567 (200) | Base: `$HOME/suwayomi/data/`<br>H2 DB: `$HOME/suwayomi/data/database/`<br>Downloads: `$HOME/suwayomi/data/downloads/`<br>Logs: `$HOME/suwayomi/data/logs/` |
| `suwayomi_filter` | Suwayomi GraphQL reading-feed filter | [`scripts/suwayomi_graphql_filter.py`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/suwayomi_graphql_filter.py) | `$PREFIX/bin/python3 $HOME/bin/suwayomi_graphql_filter.py` | `127.0.0.1:4566` (HTTP, loopback) | `GET /healthz` (200) | Logs: `$HOME/suwayomi/data/logs/graphql_filter.log` |
| `ssh` | Remote administrative shell | Termux OpenSSH package | `$PREFIX/bin/sshd -D` | `0.0.0.0:8022` (TCP, SSH) | TCP probe on 8022 (banner `SSH-`) | Config: `$PREFIX/etc/ssh/`<br>Keys: `$HOME/.ssh/authorized_keys` |
| `agy` | Autonomous AI coding agent | [`scripts/agy_session.sh`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/agy_session.sh), [`scripts/agy-runner-loop`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/agy-runner-loop), [`web/agy_index.html`](file:///home/astro/server-audit/2026-11-03/server-operations/web/agy_index.html), [`app/agy_sessions.py`](file:///home/astro/server-audit/2026-11-03/server-operations/app/agy_sessions.py) | `$PREFIX/bin/ttyd -W -a -m 8 -i 127.0.0.1 -p 7682 -b /agy -I $HOME/.config/ttyd/agy_index.html $HOME/bin/agy_session.sh` | `127.0.0.1:7682` (HTTP/WS, loopback) | `GET /agy/` (200) | Config: `$HOME/.config/ttyd/agy_index.html`<br>tmux socket: `$PREFIX/var/run/tmux-10505/agy`<br>History: PRoot Debian `/root/.local/state/` |
| `laptop_terminal` | Restricted laptop terminal bridge | [`app/laptop.py`](file:///home/astro/server-audit/2026-11-03/server-operations/app/laptop.py), [`scripts/laptop_session.py`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/laptop_session.py), [`web/laptop.js`](file:///home/astro/server-audit/2026-11-03/server-operations/web/laptop.js), [`laptop/bridge.py`](file:///home/astro/server-audit/2026-11-03/server-operations/laptop/bridge.py) | `$PREFIX/bin/ttyd -W -a -m 8 -i 127.0.0.1 -p 7683 -b /laptop $PREFIX/bin/python3 $HOME/bin/laptop_session.py` | `127.0.0.1:7683` (HTTP/WS, loopback) | `GET /laptop/` (200) | Pairing: `$HOME/.config/mobile-server-laptop/client.json`, `id_ed25519` (0600), `known_hosts` (0600) |
| `adguard` | Network DNS and ad-blocking | AdGuardHome binary | `$HOME/adguard/AdGuardHome/AdGuardHome -c $HOME/adguard/data/AdGuardHome.yaml -w $HOME/adguard/data --no-permcheck --no-etc-hosts` | `127.0.0.1:3000` (HTTP console)<br>`0.0.0.0:5353` (DNS) | `GET /` (200) | Config: `$HOME/adguard/data/AdGuardHome.yaml`<br>Data & query logs: `$HOME/adguard/data/` |
| `battery_guard` | Battery & thermal monitor | [`scripts/battery_guard.sh`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/battery_guard.sh) | `$PREFIX/bin/bash $HOME/bin/battery_guard.sh --daemon` | None (daemon process) | Process match + `$HOME/.config/battery_guard/status.json` mtime < 360s | State: `$HOME/.config/battery_guard/status.json`<br>Logs: `$HOME/.config/battery_guard/` |
| `syncthing` *(disabled)* | Continuous file synchronization | Termux Syncthing package | `$PREFIX/bin/syncthing serve --config $HOME/.local/state/syncthing --data $HOME/.local/state/syncthing --allow-newer-config --no-browser` | `127.0.0.1:8384` (HTTP, loopback) | `GET /rest/noauth/health` (200) | State & DB: `$HOME/.local/state/syncthing/` (`config.xml`, `index-*.db`, `cert.pem`, `key.pem`) |
| `vaultwarden` *(disabled)* | Bitwarden password vault | Vaultwarden binary in Debian PRoot | `$PREFIX/bin/proot-distro login debian -- /usr/bin/env ROCKET_PORT=8082 ROCKET_ADDRESS=127.0.0.1 DATA_FOLDER=$HOME/vaultwarden/data WEB_VAULT_FOLDER=$HOME/vaultwarden/web-vault $HOME/vaultwarden/vaultwarden` | `127.0.0.1:8082` (HTTP, loopback) | `GET /alive` (200) | Vault data: `$HOME/vaultwarden/data/` (`db.sqlite3`, RSA keys)<br>Web UI: `$HOME/vaultwarden/web-vault/` |
| `terminal` *(disabled)* | Admin Termux web shell | `ttyd` pkg + [`scripts/termux_session.sh`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/termux_session.sh), [`web/termux_index.html`](file:///home/astro/server-audit/2026-11-03/server-operations/web/termux_index.html) | `$PREFIX/bin/ttyd -W -i 127.0.0.1 -p 7681 -b /terminal -I $HOME/.config/ttyd/termux_index.html $HOME/bin/termux_session.sh` | `127.0.0.1:7681` (HTTP/WS, loopback) | `GET /terminal/` (200) | Config: `$HOME/.config/ttyd/termux_index.html` |
| `tor` *(disabled)* | Tor SOCKS proxy | Termux Tor pkg + [`config/torrc`](file:///home/astro/server-audit/2026-11-03/server-operations/config/torrc) | `$PREFIX/bin/tor -f $HOME/.config/tor/torrc` | `127.0.0.1:9050` (SOCKS5, loopback) | SOCKS5 handshake (`\x05\x01\x00` -> `\x05\x00`) | Config: `$HOME/.config/tor/torrc`<br>Data: `$PREFIX/var/lib/tor/` |

### On-Demand Utility

- **`converter`**: [`app/document_conversion.py`](file:///home/astro/server-audit/2026-11-03/server-operations/app/document_conversion.py). Invokes headless LibreOffice in Debian PRoot (`proot-distro login debian -- soffice --headless --convert-to pdf ...`). Runs on demand via dashboard `/api/convert/pdf`; 0 MB RAM idle footprint; uses scratch space under `$PREFIX/tmp/`.

### External Laptop Integration (Wealthfolio)

- **Ingress & Routing**: Caddy exposes port `:8444` ([`config/Caddyfile:217`](file:///home/astro/server-audit/2026-11-03/server-operations/config/Caddyfile#L217-L255)) and reverse-proxies `/wealth/*` to loopback `127.0.0.1:18930`.
- **Laptop Ownership**: Wealthfolio does **not** execute on the phone, and no financial data or statements reside on phone storage. Port `18930` is an SSH tunnel forwarded from the user's laptop. When the laptop is asleep or disconnected, Caddy returns an offline HTTP 503 fallback page.

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

Tracked repository assets mapped in canonical manifest ([`config/deployment-files.json`](file:///home/astro/server-audit/2026-11-03/server-operations/config/deployment-files.json)):
- **Source trees**: `app/`, `config/`, `gateway/`, `laptop/`, `scripts/`, `web/`.
- **Runtime helpers**: Installed to `$HOME/.local/lib/mobile-server/` (`auth.py`, `telemetry.py`, `limits.py`, `tor_proxy.py`, `laptop.py`, `service_catalog.py`, `service_panel.py`, `agy_sessions.py`, `role_templates.py`, `notes.py`, `document_conversion.py`).
- **Entrypoints**: `$PREFIX/bin/dashboard`, `$HOME/bin/server_supervisor.py`, `$HOME/bin/mobile-gateway`, and `$PREFIX/bin/server-*` wrappers.

### Excluded Sensitive Data and Runtime State

Sensitive credentials, databases, keys, user content, and session records are excluded from source tracking and release manifests (paths listed, contents omitted):
- **Credentials**: `$HOME/.config/dashboard/users.json` (mode 0600 PBKDF2 password records).
- **Session Keys**: `$HOME/.config/dashboard/` session signing keys.
- **SSH Keys & Pairing**: `$HOME/.config/mobile-server-laptop/id_ed25519` (0600), `client.json` (0600), `known_hosts` (0600); host keys in `$PREFIX/etc/ssh/ssh_host_*_key`.
- **TLS Certificates & CA Keys**: `$HOME/.local/share/caddy/pki/` (Caddy internal root/intermediate CA keys).
- **Databases**:
  - FileBrowser: `$HOME/.config/filebrowser/filebrowser.db` (SQLite).
  - Suwayomi: `$HOME/suwayomi/data/database/` (H2 database).
  - Vaultwarden: `$HOME/vaultwarden/data/db.sqlite3` and RSA signing keys.
  - AdGuard Home: `$HOME/adguard/data/` (runtime YAML with credential hashes, query logs).
  - Syncthing: `$HOME/.local/state/syncthing/` (`config.xml`, `index-*.db`, `cert.pem`, `key.pem`).
- **User Content & Media**: `/storage/emulated/0/` (Android shared storage), `$HOME/suwayomi/data/downloads/`, `$HOME/.config/dashboard/notes.json`.
- **AGY Session Logs & History**: `$HOME/.local/state/agy/`, PRoot Debian `/root/.local/state/`, active tmux sockets under `$PREFIX/var/run/tmux-10505/`.

### Namespace, UID, and Environment Conventions

- Standard paths: `$HOME` = `/data/data/com.termux/files/home`; `$PREFIX` = `/data/data/com.termux/files/usr`.
- Assumed identity: User `u0_a505` (UID `10505`).
- **Migration Boundary**: Android assigns package UIDs dynamically. An alternate UID or directory path breaks hardcoded paths and isolated socket directories (`tmux-10505`). Transferring data or software to an alternate UID or environment requires an explicit, reviewed migration rather than blind restoration.

---

## 3. Supervisor Authority and Lifecycle Contracts

All server processes run under the control of [`scripts/server_supervisor.py`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py).

### Supervisor Locks

1. **`services.lock`** (`$HOME/.config/server_recovery/services.lock`): Acquired with `fcntl.flock` during health cycles, CLI commands, and pause/resume transitions ([`scripts/server_supervisor.py:63-78`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L63-L78)). Serializes supervisor cycles and lifecycle modifications.
2. **`daemon.lock`** (`$HOME/.config/server_recovery/daemon.lock`): Held non-blocking exclusively by the running supervisor daemon ([`scripts/server_supervisor.py:389-410`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L389-L410)). Guarantees a singleton supervisor daemon.

### Exact Process Matching

- **Command-Line Matching**: [`matching_processes(sid)`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L96-L139) parses `/proc`, checks `proc.stat().st_uid == os.getuid()`, and matches explicit argv tokens. Broad `pkill` patterns and the invoking SSH session are never matched.
- **Starttime Identity**: [`process_identity(pid)`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L86-L93) reads `/proc/{pid}/stat`, skips zombies (`Z`), and captures `stat[19]` (field 22, starttime). Signals and health checks require matching this exact identity to prevent PID-reuse races.

### Process Termination Reality

[`terminate(records)`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L142-L153) issues `SIGTERM` (4-second wait), then `SIGKILL` (1-second wait).
> [!IMPORTANT]
> `terminate()` returns implicitly once its deadline expires, even if processes survive. The release adapter must explicitly recheck that matching identities are absent before replacing files or declaring quiescence.

### Per-Service Disable Markers

- Presence markers stored at `$HOME/.config/services_disabled/<sid>`.
- The supervisor cycle inspects flag existence ([`scripts/server_supervisor.py:266`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L266)); flagged services are skipped and marked `disabled`.
- Touching disables; unlinking enables. File contents and permissions are preserved without truncation.

### Global Pause Semantics

- Invoked via `server_supervisor.py --pause` (or `server-down`).
- Creates markers `$HOME/.config/server_recovery/paused` and `/storage/emulated/0/.phone-server-paused`.
- Terminates all managed services **except SSH** (port 8022 stays active for recovery).
- Calls [`stop_agy()`](file:///home/astro/server-audit/2026-11-03/server-operations/scripts/server_supervisor.py#L155-L189) to terminate AGY runner loops, PRoot descendants, and tmux panes.
- Resumed with `server_supervisor.py --resume` (or `server-up`), which removes markers and calls `ensure()`.

### Activation Adapter Status

> [!WARNING]
> The transactional release activation adapter is currently being implemented and reviewed in a separate branch (`release-activation`); it is **not available** in maintained HEAD.
> The legacy installer (`scripts/install.sh`) currently fails at entry due to baseline checks and lacks atomic rollback. File-only recovery requires stopping affected processes and ensuring service quiescence first.

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

### Supervisor Status CLI

Inspect supervisor health and registered service status without mutating state:

```sh
python3 $HOME/bin/server_supervisor.py --status
# Or via wrappers: server-health / server-status
```

Returns JSON:
- `timestamp`: Timestamp of last supervisor cycle.
- `supervisor_running`: Boolean indicating active `daemon.lock`.
- `stale`: Boolean true if status age exceeds 120s or daemon is absent.
- `services`: Map of service IDs to states (`healthy`, `recovering`, `disabled`), failure counts, and retry backoffs.

*(Audited baseline on 2026-11-03 recorded 11 healthy services and 4 intentional disabled services; do not infer current real-time uptime from static audit logs).*

### Read-Only Probes (No Credentials Required)

| Target Service | Protocol / Command | Expected Healthy Output |
| :--- | :--- | :--- |
| `caddy` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/healthz` | `200` |
| `dashboard` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/healthz` | `200` |
| `filebrowser` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8085/drive/` | `200` |
| `transfer_gateway`| `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/healthz` | `200` |
| `suwayomi` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4569/suwayomi/` | `200` |
| `suwayomi_filter` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4566/healthz` | `200` |
| `adguard` | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3000/` | `200` |
| `ssh` | TCP probe on `127.0.0.1:8022` | Banner starts with `SSH-` |
| `tor` *(if enabled)* | Send SOCKS5 greeting `\x05\x01\x00` to `127.0.0.1:9050` | Response matches `\x05\x00` |
| `syncthing` *(if enabled)* | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8384/rest/noauth/health` | `200` |
| `vaultwarden` *(if enabled)* | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8082/alive` | `200` |
| `battery_guard` | Check `$HOME/.config/battery_guard/status.json` | File exists and modified within 360s |

---

## 6. Backup and Restore Requirements

### Software Rollback vs. Data Restore

- **Software Rollback**: Restores the verified set of deployable code, scripts, static web templates, and configuration files via release manifests and file journals. Software rollback **does not restore databases, user files, credentials, or session states**.
- **Data Restore**: Replaces stateful database files and user records. Must be handled as an independent operation with distinct validation.

> [!CAUTION]
> **No Live Database Copying**: Copying database files (`filebrowser.db`, Suwayomi H2 database, Vaultwarden `db.sqlite3`) while services are active risks torn writes, corrupt WAL/journal states, and data loss.
> Configuration exports (such as `AdGuardHome.yaml` or `config.xml`) contain settings metadata, **not** database records or user content.

### Verified vs. Unverified Backup Mechanisms

- **Verified Mechanisms**:
  - The repository release engine supports verifying and snapshotting **deployable software assets and static configurations only**.
  - **No automated online database backup mechanisms currently exist** in local repository sources.
- **Unverified Procedures**:
  - Direct live copies of SQLite and H2 database files are unverified and unsafe.
  - Consistent backup procedures require an explicitly agreed **stopped-service procedure**:
    1. Acquire supervisor `services.lock`.
    2. Disable the target service via `server_supervisor.py --service <sid> --enable false`.
    3. Explicitly verify that the process identity and PID have terminated.
    4. Copy database files together with corresponding `-wal`, `-shm`, or lock files.
    5. Re-enable the service via `server_supervisor.py --service <sid> --enable true`.
- **Deferred Investigations**: Deep Suwayomi backup schedules, download cache management, and Tachiyomi cloud sync ownership are deferred to subsequent investigation phases.
