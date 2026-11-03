# Shared Service Catalog Implementation Plan

**Approved scope:** The server stabilization design already authorizes shared service identity and endpoint facts, preserving runtime behavior and dashboard appearance. This is one bounded extraction, not a new service manager.

**Goal:** Give the supervisor and dashboard one small, pure catalog for common identities and endpoint facts. Retain separate runtime launch configuration and display metadata. Delete unused dashboard copies of launcher commands and environments after proving no callers use them.

**Base:** 0ce1a30. Existing integrated proof: 49 Python tests and all actual-page browser suites pass. Truthful-memory reporting is accepted and must remain unchanged.

## Task 1: Extract and characterize common service ownership

Create `app/service_catalog.py`, modify `scripts/server_supervisor.py` and only service metadata/import composition in `app/dashboard.py`. Keep `services()` and `SERVICES_CONFIG` compatibility bindings. Update `docs/SOURCE-OWNERSHIP.md`, add focused characterization/import tests and a sanitized argv fixture if useful. Do not change HTML, Caddy, auth, conversion, notes or Suwayomi launchers.

- Catalog import performs no filesystem access, subprocess calls, network requests, secret reads, or background work.
- Expose focused builders such as `runtime_services(home, prefix)` and `dashboard_services()`. Runtime builder receives paths explicitly. Every call returns independent mutable dictionaries/lists so existing caller mutations cannot leak across uses.
- Preserve all 15 supervisor services, their insertion order, exact commands/arguments, health port/path/protocol and dynamic HOME/PREFIX behavior. Retain `services()` as a wrapper using current supervisor globals so existing tests and operational callers work.
- Preserve all nine dashboard cards, order, IDs, names, descriptions, icons, links including javascript handlers, access ports, patterns, toggle flags and converter on-demand semantics. Remove unused start_cmd/stop_cmd/env metadata only after a reference check proves they have no callers.
- Define shared identity/endpoint facts once, then compose runtime and display views. A small module with ordinary functions and dictionaries is sufficient. Avoid dataclass frameworks, dependency containers, inheritance hierarchies or universal service abstractions.
- Distinguish Suwayomi internal backend4569, filter4566 and native client4567 explicitly. Dashboard card access port4567 and fallback reachability checks4567/4569 retain current behavior. Runtime backend health stays4569 `/suwayomi/`. Preserve scheduling/filter ownership for the later Suwayomi phase.
- Retain supervisor lifecycle code without changes: exact process identity matching, locking, intentional-disabled flags, pause handling, backoff, Caddy dependency health, rotation and signal behavior.
- Bootstrap shared module imports for BOTH maintained checkout and installed layouts. Dashboard executable resides in PREFIX/bin; supervisor resides HOME/bin; shared module resides HOME/.local/lib/mobile-server. No dependency on checkout cwd or PYTHONPATH. Do not repeat the import-before-bootstrap regression found in parallel utility extraction.
- Preserve every existing comment/docstring when moving code. Preserve truthful memory reporting fields and availability behavior.
- Before extraction, establish independent runtime characterization from the existing `services()` function using source/AST or a sanitized fixture, without launching processes. Compare all commands and health fields for ordinary and alternate HOME/PREFIX values after extraction. Do not use a new table as its own oracle.
- Verify dashboard view metadata and Suwayomi distinction, fresh-copy isolation, import purity and installed-layout bootstrap. Mock OS/service effects; no real service controls, process termination, phone access, credentials, package installs, deployments, or extra agents.
- Run focused catalog tests and existing supervisor/memory/dashboard tests, then full Python once if focused checks pass. Browser suite only if changed HTTP/UI behavior requires it; no HTML changes expected. No Go rerun. Run diff --check.
- Declare new app/service_catalog.py release mapping in ignored `.superpowers/sdd/service-catalog-mappings.json` (remote HOME/.local/lib/mobile-server/service_catalog.py, mode0600). Do not change historical config/deployment-files.json or installer owned by the parallel packaging worker.
- Commit scoped implementation/tests/docs; write `.superpowers/sdd/service-catalog-report.md` with exact before/after preservation evidence, checks and remaining release integration requirement. Stop after tested local commit. Root independent review precedes integration.
