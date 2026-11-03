# Source Ownership and Architecture

## Component Responsibilities

- **`app/dashboard.py`**: Owns HTTP request composition, route dispatching, session token generation/verification, and service orchestration. It delegates role template loading to `app/role_templates.py`.
- **`app/role_templates.py`**: Owns single-source loading of installed role templates from disk (`web/` templates installed to static directory), enforcing raw byte loading without interpolation and raising structured unavailability errors when pages are missing.
- **`app/notes.py`**: Owns user quick notes persistence (`NotesStore`), including thread-safe locking, JSON file encoding, 0600 file permissions, and atomic temporary replacement.
- **`app/document_conversion.py`**: Owns batch document conversion (`convert_documents_batch`) via headless LibreOffice in proot-distro, isolated batch scratch directories, process timeout handling, safe output naming, and single PDF vs. ZIP packaging.
- **`app/service_catalog.py`**: Pure, shared single source of truth for service identity and endpoint facts. Exposes focused builders `runtime_services(home, prefix)` and `dashboard_services()`. Free of side effects, subprocess calls, network requests, filesystem access, and background work.
- **`app/auth.py`**: Owns credential verification (PBKDF2 SHA-256), login rate limiting, and cached user credential storage (`CredentialStore`) with bounded memory and thread-safe invalidation.
- **`app/limits.py`**: Owns admission control (per-endpoint semaphores) and request body size enforcement (`BODY_LIMITS`).
- **`app/telemetry.py`**: Owns system measurements, including hardware telemetry (battery, RAM, storage, CPU temperature), network metrics, and process RSS tracking.
- **`scripts/server_supervisor.py`**: Owns background process lifecycle management, periodic polling, flock-based singleton daemon and cycle locking, service failure tracking with exponential backoff, and process log rotation (2 MiB copy/truncate).
- **`config/Caddyfile`**: Owns edge reverse-proxy routing, HTTPS/TLS termination, access authentication (`forward_auth`), compression, security headers, and static asset delivery.
- **`gateway/`**: Owns high-performance streaming transport and proxying implemented in Go.
- **`web/`**: Owns frontend presentation and client-side interactions (`admin.html`, `family.html`, `login.html`, `portal.css`, `laptop.js`, `interactive_player.js`, `filebrowser.js`, `agy_index.html`, `termux_index.html`).

## Template Authority and Availability

- `app/dashboard.py` serves installed role template files from `~/.config/dashboard/static/{admin,family,login}.html` via `app/role_templates.py`.
- If an installed role template file is missing or unreadable, the server returns a controlled HTTP 503 Service Unavailable response instead of silently falling back to obsolete embedded templates.
- Reconciled maintained source ensures the primary web templates in `web/` are the single authority for deployed role page behavior.

## Native Reader and Disabled-Service Invariants

- **Native Reader Bridge (`:4567`)**: Intentionally runs without portal `forward_auth` to support native client apps (e.g., Mihon/Tachiyomi). In contrast, portal web routes under `/suwayomi` enforce admin authentication. This boundary must remain strictly preserved.
- **Disabled Services**: The intentional disabled state of `syncthing`, `vaultwarden`, `terminal`, and `tor` services (indicated by flags in `~/.config/services_disabled`) is part of stable server policy. Supervised startup must never unconditionally enable or force-start disabled services.
