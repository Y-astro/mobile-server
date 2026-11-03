# Redmi server stabilization and refactor proposal

Date: 2026-11-03
Status: Approved by the user on 2026-11-03, including implementation and brief service-scoped restarts after verification.
Target: Redmi K20 Pro, Android 11, Termux UID u0_a505, SSH port 8022 at 100.75.159.53.
Repository: /home/astro/repos/mobile-server, initial HEAD 5533fb6.

## Objective and success criteria

Make the running server reproducible from one maintained repository, isolate dashboard features so changes do not break unrelated capabilities, and retain current service behavior, appearance and user data. Optimize measured bottlenecks rather than changing resource limits speculatively. Codex selects boundaries and verifies results; AGY performs bounded investigation and implementation.

Success means the deployed custom code matches a recorded release manifest; tests exercise the actual admin and family pages; all supported request contracts remain compatible; one failed feature does not stop other dashboard features; intentional service stops remain stopped; deployment and rollback are repeatable. Suwayomi stays functional throughout the general-server work and receives a separate later investigation.

## Verified starting point

- The supervisor has 15 service definitions: 11 healthy and four intentionally disabled (Syncthing, Vaultwarden, terminal and Tor) at inspection time. Existing locking, retry backoff, process identity handling and periodic log rotation already exist.
- Eight independent health probes returned HTTP 200. This is a healthy baseline, not proof that every feature works.
- The live dashboard is a 3,383-line script with existing auth, telemetry, admission, Tor and laptop helpers. External static pages override embedded HTML.
- The repository and phone have diverged: of 21 mapped artifacts, nine differ, five match and seven are missing from repository source. Current external admin/family/login/CSS files and Suwayomi integration scripts are among the missing files.
- Existing browser tests extract older embedded HTML from the repository rather than loading the actual deployed external pages. Existing installer does not deploy those pages.
- Both live pages send PDF conversion requests to /api/convert/pdf, while backend routing and body limits recognize /api/convert. Nonempty requests encounter the generic 413 size rejection before dispatch.
- Dashboard service metadata duplicates obsolete launcher definitions. Actual controls delegate to the supervisor. Caddy duplicates portal routing for HTTP :8000 and HTTPS :8443.
- Suwayomi filter ownership overlaps between the supervisor and its launcher. Its scheduler also starts as a launcher side effect. These are recorded for the later Suwayomi phase.

## Existing harness baseline

The unchanged repository passes 35 Python tests, gateway Go race tests, and both existing Playwright browser suites. Browser tests reused installed Chromium through CHROMIUM_PATH after the default expected browser executable was absent. No dependencies were installed. These checks validate the older repository and its fixtures; reconciliation must extend them to the currently deployed pages and code. Detailed logs are stored beside this proposal.

## Approaches considered

1. **Incremental reconciliation and extraction — recommended.** Capture current behavior, reconcile live code into Git, repair contract defects, then separate responsibilities in small verified releases. This preserves working features and gives each change an explicit rollback. It takes more individual steps than a rewrite.
2. **Targeted fixes only.** Repair conversion and selected pollers while retaining current source/deployment organization. This gives faster local relief but leaves the conditions that allow future regressions.
3. **Framework or platform rewrite.** Replace the server/dashboard stack and migrate all features. This creates the largest compatibility and operational burden and has no evidence-backed benefit on this phone today.

## Proposed boundaries

### Maintained source and release ownership

Use the existing repository as the maintained source after reconciling it against the running phone. Preserve every live feature during reconciliation. Import only custom code and appropriate sanitized configuration. Keep credentials, signing keys, private certificates, databases, user files, session history and generated artifacts outside Git. Record source paths and hashes in a deployment manifest. Add contributor instructions covering ownership, service boundaries and required checks.

Retain public launcher paths and operational commands as compatibility entrypoints. Do not delete historical files until their ownership is established and the maintained release is proven. Do not restore old source copies containing credentials.

### Backend dashboard

Retain the existing standard-library HTTP server and established auth/admission helpers. Make the entrypoint compose focused modules for authentication and role checks; service status/control and telemetry; AGY/laptop sessions; and notes, clipboard and conversion utilities. Keep dependencies explicit and avoid a central mutable global context shared indiscriminately between features.

Preserve URLs, status codes, cookie handling, role boundaries, response fields and operational CLI behavior, except for explicitly tested bug fixes. Normalize /api/convert and /api/convert/pdf to the same conversion operation before body limits and admission checks. Both routes must share the same size and concurrency limits. Preserve multipart field names and output/download behavior.

### Frontend dashboard

Bring the current external admin, family and login pages and CSS into maintained source. Keep current appearance and capabilities. Separate scripts by feature where that improves state ownership. Use one explicit initialization/disposal path and shared request helpers without creating a new frontend framework.

Make polling single-flight per feature, bound request duration, pause unnecessary background polling and refresh on return. A failed stats, VPN, service or session request must affect only its own panel. Preserve unsaved notes, conversion state and active terminal behavior. Treat stale data explicitly.

Use one source for role templates. Remove or generate embedded fallbacks from the same templates after packaging checks prove that assets are complete. Missing release assets must be caught before deployment rather than silently selecting an older interface.

### Services and reverse proxy

Move shared service identity and endpoint facts into a small common catalog consumed by the supervisor and dashboard. Keep display metadata distinct from runtime launch configuration. Distinguish internal health ports from public access ports. Preserve current process identity rules, disabled flags, locks, pause/resume behavior, dependency-aware health handling and restart backoff.

Deduplicate common Caddy portal configuration only after tests establish equivalent routing, authentication, headers, upload behavior and timeouts for both :8000 and :8443. Preserve Wealthfolio :8444, FileBrowser proxy identity, gateway upload controls, raw-file response protections and all reader routes. Keep upstream services as upstream software; focus refactoring on custom integration code.

### Deployment, rollback and operations

Build a complete release artifact from maintained source, including the actual pages and helpers. Validate syntax, tests, configuration and manifest completeness before touching running services. Refuse deployment if live hashes differ from the audited baseline without reconciliation. Install code and assets coherently and keep the prior compatible release available for rollback.

Use service-scoped activation and brief staged restarts when allowed by the user's downtime preference. Keep SSH available. Do not use global pause/resume for routine dashboard updates. If health or compatibility verification fails, restore the prior code/assets/configuration together. Do not migrate, overwrite or roll back databases as part of this software refactor.

Document durable data paths, restore requirements and existing backup mechanisms. Any data backup operation must use the service's supported consistent backup method or an agreed stop window. This design does not authorize raw copies of live databases or starting disabled services.

## Execution sequence

1. Record and preserve current runtime behavior and a sanitized release manifest. Reconcile live changes and missing files into the existing repository.
2. Update fixtures and tests to exercise reconciled source and actual pages. Establish a baseline that represents the current phone.
3. Repair confirmed conversion and packaging contracts with focused regression checks.
4. Extract backend responsibilities and frontend feature state in independently reviewable changes, preserving behavior around each extraction.
5. Consolidate service metadata and equivalent proxy configuration while preserving lifecycle and access invariants.
6. Complete coherent deployment/rollback and operations documentation. Apply bounded releases, verify each service, then compare measurements against baseline.
7. Start the separate Suwayomi phase after the rest is stable.

AGY workers may run independent code tasks in separate worktrees. Only one deployment owner may change phone files or restart services. Codex independently accepts source, behavior and runtime evidence before each release is considered complete.

## Verification and acceptance

- Reuse existing Python, Go race and Playwright suites. Add tests for behavior and cross-feature failure cases rather than tests that merely mirror implementation.
- Load actual maintained admin/family pages in browser tests. Exercise conversion, notes, service controls and sessions through user interactions with synthetic fixtures. Test role denial and session/error handling. Do not expose real credentials or user records in test artifacts.
- Verify conversion route aliases, multipart contract, size limits, admission release after failure and absence of duplicate conversion work.
- Simulate delayed/failed polling endpoints and confirm bounded requests, independent panel recovery and preserved user state.
- Verify supervisor intentional-disable behavior, pause/resume, exact process matching, retry backoff and existing log handling.
- Compare effective Caddy routes and auth behavior on HTTP and HTTPS portals, native reader bridge and Wealthfolio endpoint. Use existing trust configuration for HTTPS verification.
- Test release completeness, refusal on unexpected live drift and rollback using isolated fixtures before phone activation.
- After activation, verify fresh supervisor state, preserved disabled flags, GET health/auth checks and appropriate user-facing interactions. Use temporary synthetic files or sessions only when needed, with cleanup.
- Compare dashboard response latency, request counts and memory under the same bounded workload. Do not claim improved performance from a single sample or set arbitrary JVM/memory targets.

## Suwayomi invariants and deferred work

Preserve backend :4569, filter :4566, native-client bridge :4567, portal /suwayomi, cached thumbnail routing, Category 1 Reading behavior and midnight IST scheduler behavior. Preserve existing native-client access policy, library, downloads, database, extensions and JVM settings during general-server work. Capture integration scripts in maintained source; defer their behavioral refactor.

The later Suwayomi phase will examine launcher/filter/scheduler ownership, cache correctness, GraphQL compatibility, update scheduling, memory under representative workloads and supported backup/restore. The audit's single-sample JVM resource number is not evidence for changing heap or database settings now.

## Constraints and remaining uncertainty

The user explicitly approved this written design and implementation on 2026-11-03. Brief service-scoped restarts after verification are authorized. No repeated approval is required for work within this design. Existing appearance is preserved. No framework migration, hardware purchase, upstream rewrite or unrelated feature expansion is included.

No authenticated live interaction or load benchmark has been performed. Exact user-reported regression examples have not yet been supplied. Poller-induced starvation is a hypothesis, while source/deployment drift and conversion contract mismatch are confirmed. Disabled-service runtime health, including Vaultwarden's PRoot path resolution, remains unverified without starting those services.
