# Service Panel Ownership Plan

Approved scope: isolate backend feature responsibilities while preserving request contracts, truthful memory, supervisor lifecycle behavior, disabled flags and Suwayomi behavior. This is ownership extraction, not performance tuning or deep Suwayomi work.

Base: 5d36e99. Combined124 Python tests pass (two expected offline Caddy skips). Source-only release candidate validates40 entries. No phone changes.

## Task 1: Service reporting and control owner

Create a small app/service_panel.py owner with explicit catalog, disabled-directory, supervisor status-file and supervisor-command dependencies. Keep process-ID/RSS/port probes small and independently testable. Reporting and toggle logic belong here; the HTTP server composes it and retains compatibility wrappers. Constructor performs no I/O, process creation, network requests or background work.

Keep dashboard SERVICES_CONFIG and wrappers get_service_pids(pattern), get_process_rss(pid), is_service_port_open(port), get_services_report(force=False), toggle_service(sid, enable=None). Existing tests patch these names; use explicit lazy callbacks at composition so overrides reach report/control paths. Avoid module introspection, importing dashboard from helper, or passing a global application context. Standard-library module mocks remain scoped and restored. Four report dependencies (PID/RSS/port/unread) are sufficient; do not build a dependency injection framework.

Preserve all current report field names, service order, port logic, health/disabled checks, ondemand behavior, memory completeness and aggregate rules, badges, error strings and force propagation. No invented memory. Unknown/partial RSS remains None; known zero remains zero. Disabled services receive no probes. Existing Suwayomi cached unread helpers remain in dashboard for later Suwayomi work; inject that callable into owner and preserve alternate-port probing from existing catalog. Do not change GraphQL/category/cache/scheduler semantics.

Preserve exact supervisor argv, capture_output/text/timeout25, target determination for enable=None/True/False and truthiness, ondemand return behavior, error mapping and enable wait15x0.1 seconds. Existing supervisor remains sole lifecycle authority: no new process signals, restart loops, locks, flags or service manager. Do not alter auth, routes, telemetry sampling, laptop, AGY, notes, conversion, templates, catalog, Caddy, installer or release mechanism.

Characterize existing functions before extraction using deterministic synthetic snapshots and command traces. Keep all retained/moved comments and docstrings. Dashboard differences should be import/binding/wrapper bodies and moved code removal only. HTTP output and surrounding methods remain identical. Bootstrap helper import follows MOBILE_LIB setup. Flat helper auto-maps0600; historical35-row baseline and pin unchanged. Update ownership docs and installed-layout required-helper assertion.

Tests use temporary directories and fake probes/subprocesses only. Cover healthy/disabled/ondemand/sleeping, unread force, measured zero/partial/unavailable totals, status-file errors, unknown/ondemand toggle, disabled inversion/explicit intent, subprocess failure/timeout, exact argv/timeouts and bounded enable polling. Add loopback HTTP list/control compatibility assertions only if missing. Preserve existing tests; do not weaken their patch seams. Never use real pgrep, /proc reads, ports, tmux/proot/supervisor or phone during tests.

Run nearest owner + service-memory + dashboard + installed-layout checks first; then full Python once, generate AND validate source manifest, git diff --check. No browser/Go needed for unchanged page/HTTP semantics. Commit bounded source/tests/docs and write ignored .superpowers/sdd/service-panel-owner-report.md with characterization evidence and module mapping. Stop for independent review and Codex acceptance; no deployment.
