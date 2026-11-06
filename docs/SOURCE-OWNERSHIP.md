# Source Ownership and Architecture

## Component Responsibilities

- **`app/dashboard.py`**: Owns HTTP request composition, route dispatching, session token generation/verification, and service orchestration. It delegates role template loading to `app/role_templates.py`, AGY tmux session management to `app/agy_sessions.py`, and service status reporting and lifecycle toggling to `app/service_panel.py`.
- **`app/service_panel.py`**: Pure owner for service reporting and control (`ServicePanel`), managing process RSS/port/PID inspection, supervisor command delegation, disabled service invariants, and Suwayomi cache composition with explicit dynamic callbacks.
- **`app/agy_sessions.py`**: Pure owner for AGY tmux instance operations (`AgySessions`), managing command discovery/fallback, session listing, locked instance creation up to 8 instances, and secondary session termination without direct HTTP coupling.
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
- **`web/`**: Owns frontend presentation and client-side interactions (`admin.html`, `family.html`, `login.html`, `portal.css`, `laptop.js`, `interactive_player.js`, `filebrowser.js`, `agy_index.html`, `termux_index.html`). Implements the dark Material 3 design system (`--md-sys-color-*` tokens, 4px grid rhythm, sans typography, tabular numerals, 48px minimum touch targets) across administrator, family, and login pages.
- **`tests/material3-browser.cjs`**: Owns automated visual and interaction acceptance for the Material 3 portal, testing computed tokens, typography, 48px touch targets, responsive viewports (1280px desktop, 360–412px mobile) without overflow, complete service grid states, dialog keyboard flows, and notes persistence.

## Antra Ownership

- `app/antra_store.py` owns the durable queue, immutable private/family destinations, publication checkpoints, and public record filtering. Changes affect both the dashboard and worker.
- `app/antra_client.py` owns authenticated provider HTTP/WebSocket traffic. Credentials and cookies stay on the phone; browser code never talks to the provider directly.
- `app/antra_worker.py` owns background processing, quota scheduling, replay recovery, audio validation, safe filesystem publication, and acknowledgements. It is the independently supervised `antra` service.
- `app/antra_http.py` owns administrator-only page/API dispatch. The dashboard passes its existing authorization callback; the dispatcher must not import the legacy dashboard module.
- `web/antra.html`, `web/antra.css`, and `web/antra.js` own the page. They reuse shared appearance and polling assets. Family users cannot access the page or change its queue.
- `tools/split_music_storage.py` owns the completed data migration and its private recovery journal. Source rollback must preserve the split and all downloaded user files.
- The initial catalog addition uses the reviewed `antra-service` activation profile. Subsequent ordinary updates use `general`; adding another service requires its own reviewed transition.

## Role Template Authority and Availability

- `app/dashboard.py` serves installed role template files from `~/.config/dashboard/static/{admin,family,login}.html` via `app/role_templates.py`.
- If an installed role template file is missing or unreadable, the server returns a controlled HTTP 503 Service Unavailable response instead of silently falling back to obsolete embedded templates.
- Reconciled maintained source ensures the primary web templates in `web/` are the single authority for deployed role page behavior.

## Native Reader and Disabled-Service Invariants

- **Native Reader Bridge (`:4567`)**: Intentionally runs without portal `forward_auth` to support native client apps (e.g., Mihon/Tachiyomi). In contrast, portal web routes under `/suwayomi` enforce admin authentication. This boundary must remain strictly preserved.
- **Disabled Services**: The intentional disabled state of `syncthing`, `vaultwarden`, `terminal`, and `tor` services (indicated by flags in `~/.config/services_disabled`) is part of stable server policy. Supervised startup must never unconditionally enable or force-start disabled services.
