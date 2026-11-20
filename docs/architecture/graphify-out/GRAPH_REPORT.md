# Graph Report - mobile-server  (2026-11-20)

## Corpus Check
- 38 files · ~45,000 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 860 nodes · 1824 edges · 56 communities (49 shown, 7 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 45 edges (avg confidence: 0.86)
- AGY run usage: 464,595 input tokens · 72,014 output tokens · 9,198,703 cache-read tokens.
- AST extraction is deterministic; semantic extraction usage was not measured separately.

## Community Hubs (Navigation)
- Release Management & Atomic Activation Engine
- Release Management & Atomic Activation Engine
- Antra Music Downloader & Persistent Queue
- Suwayomi Manga & GraphQL Filtering Pipeline
- main.go Subsystem
- Antra Music Downloader & Persistent Queue
- server_supervisor.py Subsystem
- Core Web Portal & Role Authentication
- Release Management & Atomic Activation Engine
- Wallpapers & Material Theme Extraction
- Antra Music Downloader & Persistent Queue
- Suwayomi Manga & GraphQL Filtering Pipeline
- Core Web Portal & Role Authentication
- Antra Music Downloader & Persistent Queue
- Release Management & Atomic Activation Engine
- Core Web Portal & Role Authentication
- Remote Laptop Terminal Bridge
- Suwayomi Manga & GraphQL Filtering Pipeline
- Antra Music Downloader & Persistent Queue
- Core Web Portal & Role Authentication
- Path Subsystem
- Core Web Portal & Role Authentication
- LifecycleDriver Subsystem
- Core Web Portal & Role Authentication
- Antra Music Downloader & Persistent Queue
- Suwayomi Manga & GraphQL Filtering Pipeline
- Any Subsystem
- Core Web Portal & Role Authentication
- Remote Laptop Terminal Bridge
- Navidrome Music Streaming & PRoot SSO
- Hardware Guardians & Telemetry Monitoring
- auth.py Subsystem
- Antra Music Downloader & Persistent Queue
- CredentialStore Subsystem
- agy_session.sh Subsystem
- TermuxLifecycleDriver Subsystem
- Ingress Routing & Reverse Proxy Gateway
- termux_session.sh Subsystem
- Core Web Portal & Role Authentication
- TorProxy Subsystem
- Ingress Routing & Reverse Proxy Gateway
- Suwayomi Manga & GraphQL Filtering Pipeline
- Navidrome Music Streaming & PRoot SSO
- Core Web Portal & Role Authentication
- Core Web Portal & Role Authentication
- start-services.sh Subsystem
- RoleTemplateUnavailable Subsystem
- Ingress Routing & Reverse Proxy Gateway
- QuiescenceError Subsystem
- Antra Music Downloader & Persistent Queue
- Antra Music Downloader & Persistent Queue
- Watchdog: phone-server-guardian.sh Subsystem
- install.sh Subsystem
- phone-server-guardian.sh Subsystem
- Secret: AdGuardHome.yaml Runtime Credentials Subsystem
- Ingress Routing & Reverse Proxy Gateway

## God Nodes (most connected - your core abstractions)
1. `AntraStore` - 32 edges
2. `LifecycleDriver` - 26 edges
3. `apply()` - 25 edges
4. `TermuxLifecycleDriver` - 24 edges
5. `AntraClient` - 22 edges
6. `Supervisor: server_supervisor.py` - 21 edges
7. `AntraWorker` - 20 edges
8. `ActivationError` - 20 edges
9. `TransactionError` - 19 edges
10. `ReleaseTransaction` - 19 edges
11. `AntraClientError` - 18 edges
12. `_rollback_internal()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `Client: Native Subsonic & Tachiyomi/Mihon` --CONNECTS_DIRECT_TOKEN--> `Service: Navidrome (Debian PRoot Music)`  [EXTRACTED]
  docs/OPERATIONS.md → app/navidrome.py
- `Service: Dashboard (Core Portal)` --SIGNS_SESSIONS_WITH--> `Secret: Dashboard Session Signing Keys`  [EXTRACTED]
  app/dashboard.py → docs/OPERATIONS.md
- `Service: Dashboard (Core Portal)` --AUTHENTICATES_AGAINST--> `Secret: users.json (PBKDF2 Hashes)`  [EXTRACTED]
  app/dashboard.py → docs/OPERATIONS.md
- `Service: Dashboard (Core Portal)` --SERVES_THEMES--> `Storage: $HOME/wallpapers/ (Wallpapers & Themes)`  [EXTRACTED]
  app/dashboard.py → docs/WALLPAPERS.md
- `Service: Navidrome (Debian PRoot Music)` --READS_PRIVATE--> `Storage: Music/Astro (Private Music Library)`  [EXTRACTED]
  app/navidrome.py → docs/OPERATIONS.md
- `Service: Navidrome (Debian PRoot Music)` --READS_SHARED--> `Storage: Music/Family (Shared Music Library)`  [EXTRACTED]
  app/navidrome.py → docs/OPERATIONS.md
- `Service: Navidrome (Debian PRoot Music)` --RUNS_IN--> `Container: Debian PRoot Rootfs`  [EXTRACTED]
  app/navidrome.py → docs/OPERATIONS.md
- `Service: Suwayomi Server (Manga Backend)` --DOWNLOADS_TO--> `Storage: $HOME/suwayomi/data/downloads`  [EXTRACTED]
  scripts/start_suwayomi.sh → docs/OPERATIONS.md

## Import Cycles
- None detected.

## Communities (56 total, 7 thin omitted)

### Community 0 - "Release Management & Atomic Activation Engine"
Cohesion: 0.06
Nodes (83): apply(), ApplyError, commit(), compute_file_sha256(), fsync_dir(), get_canonical_target_mode(), is_entrypoint(), is_forbidden_secret_or_runtime_path() (+75 more)

### Community 1 - "Release Management & Atomic Activation Engine"
Cohesion: 0.07
Nodes (74): importlib_util, inspect, ActivationError, ActivationLock, ActivationLockError, apply(), capture_prior_backend_recipe(), compute_file_sha256() (+66 more)

### Community 2 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.09
Nodes (24): AntraStore, AntraStoreError, canonical_music_url(), InvalidTransitionError, JobNotFoundError, Any, Exception, Path (+16 more)

### Community 3 - "Suwayomi Manga & GraphQL Filtering Pipeline"
Cohesion: 0.07
Nodes (29): http_client, http_server, logging_handlers, apply_reading_constraints(), background_refresher(), CacheColdError, clean_request_headers(), clean_response_headers() (+21 more)

### Community 4 - "main.go Subsystem"
Cohesion: 0.06
Nodes (33): admission, bufferPool, idleBody, idleWriter, limitedConn, limitedListener, gateway(), main() (+25 more)

### Community 5 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.14
Nodes (16): AntraClientError, ConcurrencyLimitError, DailyLimitError, Any, Exception, Raised when upstream returns a 429 daily limit., Raised when a job submission timed out or failed without a confirmed job_id., Fetch authenticated user profile details from /api/auth/me. (+8 more)

### Community 6 - "server_supervisor.py Subsystem"
Cohesion: 0.22
Nodes (19): errno, atomic_json(), control(), ensure(), get_scheduler_pid_file(), get_tracer_pid(), healthy(), initialize() (+11 more)

### Community 7 - "Core Web Portal & Role Authentication"
Cohesion: 0.13
Nodes (16): get_services_report(), toggle_service(), dashboard_services(), Any, Single source of truth for service identities, endpoint facts, and views.…, Return fallback reachability ports for Suwayomi probing [4569, 4567]., Build supervisor runtime configuration for all managed services. Args: home:…, Build dashboard display and toggle metadata for user-facing service cards.… (+8 more)

### Community 8 - "Release Management & Atomic Activation Engine"
Cohesion: 0.18
Nodes (20): compute_sha256_and_size(), generate_manifest(), get_canonical_ownership(), is_forbidden_secret_or_runtime_path(), load_baseline_mapping(), main(), ManifestError, Any (+12 more)

### Community 9 - "Wallpapers & Material Theme Extraction"
Cohesion: 0.18
Nodes (13): CorruptLibraryError, dispatch_wallpapers(), _is_valid_pos_int(), Any, Exception, Path, Wallpaper library reader and HTTP request dispatcher. Provides manifest…, Base error for wallpaper operations. (+5 more)

### Community 10 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.15
Nodes (15): main(), maybe_reexec_dns_fix(), Antra background download worker. Supervised worker process for durable Antra…, Re-exec process with LD_PRELOAD libdns_fix.so if in real Termux and not already…, builtins, contextlib, ctypes, fcntl (+7 more)

### Community 11 - "Suwayomi Manga & GraphQL Filtering Pipeline"
Cohesion: 0.23
Nodes (17): argparse, datetime, daemonize(), get_daemon_pid(), get_next_midnight(), log(), main(), print_status() (+9 more)

### Community 12 - "Core Web Portal & Role Authentication"
Cohesion: 0.12
Nodes (12): get_reading_manga_ids(), get_suwayomi_unread_cached(), In-flight work admission for dashboard uploads and conversions., Stateless reader for installed role templates. Provides single-source loading…, auth, document_conversion, email_parser, email_policy (+4 more)

### Community 13 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.17
Nodes (7): AntraClient, MissingCredentialsError, Path, Outbound client for Antra upstream services., Authenticate with upstream Antra using configured credentials., Acknowledge a durable local write to upstream. Never raises on failure., Raised when credentials.json is missing or incomplete.

### Community 14 - "Release Management & Atomic Activation Engine"
Cohesion: 0.18
Nodes (8): AntraWorker, Any, Background download worker orchestrating jobs, batches, and files., Acquire singleton process execution lock., Validate fixed output roots and the narrowed live family scope., Keep one folder-mode upstream job for the whole playlist, including after…, Process one runnable job if available., Main loop executed by supervised worker process.

### Community 15 - "Core Web Portal & Role Authentication"
Cohesion: 0.16
Nodes (8): LoginLimiter, Bound failures per source and per account, and bound stored keys., get_filebrowser_jwt(), sign_session_token(), convert_documents_batch(), file_list: list of tuples (original_filename, file_bytes) Returns:…, Admission, canonical_post_path()

### Community 16 - "Remote Laptop Terminal Bridge"
Cohesion: 0.25
Nodes (11): Bounded phone-side client for the laptop's restricted Tailscale SSH bridge., Sample actual host telemetry once, independently of HTTP request volume., Control a real Tor SOCKS proxy and report verified proxy egress only., copy, json, os, pathlib, ttyd URL argument is an ID, never a shell command. (+3 more)

### Community 17 - "Suwayomi Manga & GraphQL Filtering Pipeline"
Cohesion: 0.12
Nodes (16): Boot: Termux:Boot & JobScheduler, Config: deployment-files.json, Lock: daemon.lock & services.lock, Secret: Laptop ed25519 Pairing Keys, Secret: SSH Host & Authorized Keys, Service: AdGuard Home (DNS & Filtering), Service: AGY (Agent Terminal), Service: Battery Guard (Thermal & Battery) (+8 more)

### Community 18 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.21
Nodes (11): Antra upstream HTTP and WebSocket client. Handles authenticated API…, HTTP request dispatcher for Antra administration endpoints. Enforces…, Antra SQLite queue and public state store. Shared by API dispatcher and…, http_cookiejar, re, socket, sqlite3, ssl (+3 more)

### Community 19 - "Core Web Portal & Role Authentication"
Cohesion: 0.31
Nodes (5): DashboardHandler, is_admin(), verify_session_token(), error(), BaseHTTPRequestHandler

### Community 20 - "Path Subsystem"
Cohesion: 0.18
Nodes (12): _compute_sha256(), _execute_supervisor(), load_isolated_lifecycle(), Path, Validate candidate runtime services against prior services for the given…, Validate candidate catalog file directly against prior catalog file., Preflight verification: assert candidate service catalog matches prior runtime…, Load preserved supervisor and service catalog from recovery directory in an… (+4 more)

### Community 21 - "Core Web Portal & Role Authentication"
Cohesion: 0.15
Nodes (12): get_process_rss(), get_service_pids(), is_service_port_open(), probe_process_rss(), probe_service_pids(), probe_service_port_open(), app/service_panel.py: Service reporting and control owner., Find process IDs matching pattern, excluding current process. (+4 more)

### Community 23 - "Core Web Portal & Role Authentication"
Cohesion: 0.23
Nodes (7): AgySessions, Any, Manages AGY tmux sessions and command selection., close_agy_session(), create_agy_session(), get_tmux_cmd(), list_agy_sessions()

### Community 24 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.18
Nodes (10): ensure_track_parent(), file_digest(), publish_noreplace(), Path, Use Linux's atomic RENAME_NOREPLACE; unsupported mounts fail without…, Publish only verified audio; durable checkpoints recover rename-before-commit…, Inspect FLAC STREAMINFO header to verify valid native FLAC bit depth (16-bit or…, sanitize_track_path() (+2 more)

### Community 25 - "Suwayomi Manga & GraphQL Filtering Pipeline"
Cohesion: 0.24
Nodes (6): extract_python_script_and_args(), Parse python command line tokens to extract script path and trailing arguments.…, Match owned navidrome launcher or proot container processes., Trusted native matching for owned antra worker processes. Matches owned…, Trusted native matching for owned suwayomi_scheduler processes. Matches owned…, Locate supervisor daemon process by exact script binding and valid python argv.…

### Community 26 - "Any Subsystem"
Cohesion: 0.18
Nodes (3): Any, Load supervisor module in an isolated context with bound state paths., Reload supervisor module to bind newly applied code and configuration.

### Community 27 - "Core Web Portal & Role Authentication"
Cohesion: 0.31
Nodes (4): get_battery_telemetry(), get_stats(), bounded_command(), Telemetry

### Community 28 - "Remote Laptop Terminal Bridge"
Cohesion: 0.36
Nodes (4): LaptopClient, LaptopError, Exception, validate_id()

### Community 29 - "Navidrome Music Streaming & PRoot SSO"
Cohesion: 0.24
Nodes (8): main(), Path, Navidrome service launcher for mobile-server environment. Validates the…, verify_binary(), fnmatch, hashlib, release-manifest.py: Deterministic release manifest generation and offline…, sys

### Community 30 - "Hardware Guardians & Telemetry Monitoring"
Cohesion: 0.49
Nodes (9): check_battery(), log(), run_daemon(), send_notification(), battery_guard.sh script, show_status(), speak_alert(), stop_daemon() (+1 more)

### Community 31 - "auth.py Subsystem"
Cohesion: 0.22
Nodes (6): app/agy_sessions.py: Multi-instance AGY session management via tmux., Password verification and bounded login throttling; no plaintext credentials., base64, glob, hmac, secrets

### Community 32 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.33
Nodes (9): is_supported_music_url(), Validate that url is an HTTPS URL matching the supported music allowlist., dispatch_antra(), _is_full_same_origin(), Any, Route and dispatch Antra API and page requests., Validate full effective origin (scheme + host + port) for state-modifying…, _send_error() (+1 more)

### Community 33 - "CredentialStore Subsystem"
Cohesion: 0.22
Nodes (3): CredentialStore, hash_password(), verify_password()

### Community 34 - "agy_session.sh Subsystem"
Cohesion: 0.22
Nodes (8): HOME, PATH, PREFIX, agy_session.sh script, TERM, TMPDIR, TMUX_TMPDIR, USER

### Community 35 - "TermuxLifecycleDriver Subsystem"
Cohesion: 0.25
Nodes (3): Production lifecycle driver invoking scripts/server_supervisor.py., Start daemon using the restored LIVE home/bin/server_supervisor.py script., TermuxLifecycleDriver

### Community 36 - "Ingress Routing & Reverse Proxy Gateway"
Cohesion: 0.29
Nodes (8): Bridge: Wealthfolio Laptop Integration, Config: Caddyfile, Listener: Caddy HTTP (:8000), Listener: Caddy HTTPS (:8443), Listener: Caddy Wealthfolio (:8444), Route: Caddy forward_auth -> :8001/auth/verify, Route: /music/* -> Navidrome :4533, Service: Caddy (Edge Reverse Proxy)

### Community 37 - "termux_session.sh Subsystem"
Cohesion: 0.25
Nodes (7): HOME, LD_PRELOAD, PATH, PREFIX, termux_session.sh script, TERM, TMPDIR

### Community 38 - "Core Web Portal & Role Authentication"
Cohesion: 0.29
Nodes (3): run_server(), ThreadedServer, ThreadingHTTPServer

### Community 40 - "Ingress Routing & Reverse Proxy Gateway"
Cohesion: 0.38
Nodes (7): Client: Native Subsonic & Tachiyomi/Mihon, Listener: Caddy Suwayomi Direct (:4567), Route: /suwayomi/* -> Filter :4566 / Backend :4569, Service: Suwayomi Server (Manga Backend), Service: Suwayomi Filter (GraphQL Proxy), Storage: $HOME/suwayomi/data/downloads, Storage: $HOME/suwayomi/data/.../thumbnails

### Community 41 - "Suwayomi Manga & GraphQL Filtering Pipeline"
Cohesion: 0.29
Nodes (6): HOME, LD_PRELOAD, PATH, PREFIX, start_suwayomi.sh script, TMPDIR

### Community 42 - "Navidrome Music Streaming & PRoot SSO"
Cohesion: 0.33
Nodes (7): Service: Antra Music Downloader, Service: Navidrome (Debian PRoot Music), Database: antra/queue.sqlite3, Storage: Music/Astro (Private Music Library), Storage: Music/Family (Shared Music Library), Container: Debian PRoot Rootfs, Utility: LibreOffice Document Converter

### Community 43 - "Core Web Portal & Role Authentication"
Cohesion: 0.33
Nodes (3): get_user_notes(), set_user_notes(), NotesStore

### Community 44 - "Core Web Portal & Role Authentication"
Cohesion: 0.33
Nodes (6): Role: Admin User (Astro), Role: Family User, Secret: Dashboard Session Signing Keys, Secret: users.json (PBKDF2 Hashes), Service: Dashboard (Core Portal), Storage: $HOME/wallpapers/ (Wallpapers & Themes)

### Community 45 - "start-services.sh Subsystem"
Cohesion: 0.33
Nodes (5): HOME, PATH, PREFIX, start-services.sh script, TMPDIR

### Community 46 - "RoleTemplateUnavailable Subsystem"
Cohesion: 0.40
Nodes (5): Exception, Raised when a required role template is missing, unreadable, or not a regular…, Read raw bytes of the requested role template from static_dir. Args:…, read_role_template(), RoleTemplateUnavailable

### Community 47 - "Ingress Routing & Reverse Proxy Gateway"
Cohesion: 0.40
Nodes (5): Route: /drive/* -> Gateway :8081 -> FB :8085, Service: FileBrowser (Cloud Storage), Service: Transfer Gateway (Admission Proxy), Database: filebrowser.db, Storage: /storage/emulated/0/ (Android Shared)

### Community 48 - "QuiescenceError Subsystem"
Cohesion: 0.40
Nodes (4): RuntimeError, QuiescenceError, Raised when exact process selection or verified shutdown is unsafe., Stop supervisor daemon with verified termination before returning.

### Community 50 - "Antra Music Downloader & Persistent Queue"
Cohesion: 0.50
Nodes (3): MissingDependencyError, Connect to WebSocket event stream for job and yield events with bounded…, Raised when a declared external dependency (e.g. websockets) is absent.

### Community 51 - "Watchdog: phone-server-guardian.sh Subsystem"
Cohesion: 0.50
Nodes (4): OS: Tailscale VPN (com.tailscale.ipn), OS: Termux App (UID 10505 u0_a505), OS Invariant: com.xiaomi.finddevice, Watchdog: phone-server-guardian.sh

## Knowledge Gaps
- **55 isolated node(s):** `phone-server-guardian.sh script`, `start-services.sh script`, `HOME`, `PREFIX`, `PATH` (+50 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 351 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.