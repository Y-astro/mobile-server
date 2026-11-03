# Source Ownership and Architecture

## Component Responsibilities

- **`app/dashboard.py`**: Owns HTTP request composition, route dispatching, session token generation/verification, and service orchestration. It embeds fallback HTML templates but prefers external templates on disk when present.
- **`app/auth.py`**: Owns credential verification (PBKDF2 SHA-256), login rate limiting, and cached user credential storage (`CredentialStore`) with bounded memory and thread-safe invalidation.
- **`app/limits.py`**: Owns admission control (per-endpoint semaphores) and request body size enforcement (`BODY_LIMITS`).
- **`app/telemetry.py`**: Owns system measurements, including hardware telemetry (battery, RAM, storage, CPU temperature), network metrics, and process RSS tracking.
- **`scripts/server_supervisor.py`**: Owns background process lifecycle management, periodic polling, flock-based singleton daemon and cycle locking, service failure tracking with exponential backoff, and process log rotation (2 MiB copy/truncate).
- **`config/Caddyfile`**: Owns edge reverse-proxy routing, HTTPS/TLS termination, access authentication (`forward_auth`), compression, security headers, and static asset delivery.
- **`gateway/`**: Owns high-performance streaming transport and proxying implemented in Go.
- **`web/`**: Owns frontend presentation and client-side interactions (`admin.html`, `family.html`, `login.html`, `portal.css`, `laptop.js`, `interactive_player.js`, `filebrowser.js`, `agy_index.html`, `termux_index.html`).

## Template Fallback Hierarchy

- `app/dashboard.py` serves external template files from `~/.config/dashboard/static/{admin,family,login}.html` when available.
- If external files are missing or unreadable, the server silently falls back to embedded HTML strings (`ADMIN_DASHBOARD_HTML`, `FAMILY_DASHBOARD_HTML`, `LOGIN_HTML`).
- Reconciled maintained source ensures the primary web templates in `web/` reflect current deployed behavior.

## Native Reader and Disabled-Service Invariants

- **Native Reader Bridge (`:4567`)**: Intentionally runs without portal `forward_auth` to support native client apps (e.g., Mihon/Tachiyomi). In contrast, portal web routes under `/suwayomi` enforce admin authentication. This boundary must remain strictly preserved.
- **Disabled Services**: The intentional disabled state of `syncthing`, `vaultwarden`, `terminal`, and `tor` services (indicated by flags in `~/.config/services_disabled`) is part of stable server policy. Supervised startup must never unconditionally enable or force-start disabled services.
