# Suwayomi reliability implementation plan

Approved design: [Suwayomi reliability](../specs/2026-11-04-suwayomi-reliability-design.md).
General-server runtime release `0cf104a` remains the deployment baseline. The
historical 35-row ownership manifest and its hash remain immutable.

The referenced writing-plans skill is not installed in the local skills or plugin
directories. This plan records the required decisions directly. Use external AGY
Gemini 3.8 Flash High implementers; Codex reviews and owns phone activation. Reuse
the existing Python, browser, release-manifest and native recovery harnesses.
Each worker receives only its bounded task brief and inherited contracts.

## Task 1: correct filter/cache contracts

Files: `scripts/suwayomi_graphql_filter.py` and a focused filter test module.
Use an isolated worktree. No SSH, phone writes, dependency installation, server
restart or changes to Caddy, DNS, launcher, scheduler or UI in this task.

- Make import side-effect-free. Preserve the executable entry point and runtime
  logging/ports. Separate pure request decisions and one cache owner from I/O.
- Preserve the recognized feed heuristic and unrelated request behavior. Validate
  matching request object, variables and filter shapes. Keep intended Reading,
  unread and in-library constraints for a successful empty result too.
- Distinguish never-loaded cache, a successful empty/missing category and a failed
  lookup. Retain last successful IDs on transient failure; with no successful
  snapshot, return a controlled retryable error for a recognized feed.
- Keep the existing 30-second TTL. Coalesce concurrent refreshes. Do not refresh
  a known-empty category on every read. Bound lookup waits and retry frequency;
  background refresh must not create a storm or block unrelated requests.
- Preserve upstream HTTP status/body and relevant headers for GET and POST,
  including HTTPError. Emit one final content length, remove hop-by-hop and
  conflicting generated headers, encode generated errors with json.dumps.
  Retain HTTP/1.0 in this slice. Do not broaden Caddy/GET feed matching.
- Prove behavior against a fake upstream, including malformed/null/error/empty
  categories, stale-good failure, cold failure, concurrent empty-cache readers,
  exact non-feed pass-through, HTTP errors, headers and valid JSON errors.
  No import-time runtime file opens, process launches or networking are allowed.

Commit passing source/tests. Codex reviews the diff and one decisive input-driven
HTTP/cache check before integration. Record spec compliance and code quality.

## Task 2: add a narrow filter release profile

Files: activation adapter, installer parser and focused activation/installer tests.
Preparation may run alongside Task 1 in an isolated worktree because its control
code and tests are independent of filter internals. Integrate and deploy only
after both tasks pass acceptance. Keep the default general-server protections.
Do not modify the file transaction engine or broaden AGY/Suwayomi ownership.

- Add an explicit `suwayomi-filter` profile for plan/apply. Its only permitted
  changed owned runtime destination is the existing filter script. Select exactly
  `suwayomi_filter`; do not stop Java, Caddy, dashboard or supervisor.
- General profile continues refusing all changed Suwayomi runtime assets.
  Profile selection is strict and flows from CLI to plan/apply and persisted
  activation journal. Recovery reads the validated journal profile, not a new
  caller override. Absent profile in a legacy journal means general; malformed
  or unknown profiles fail closed.
- Bind profile to recomputed changed destinations, service set and recovery
  assets. Profile mutation must never turn another component's file changes into
  an allowed filter release. Preserve the existing hash/bootstrap/flag checks.
- Extend installer quoting/duplicate/missing/invalid argument checks for the new
  option. Preserve no-argument fail-fast behavior and native Termux shebang.
- Prove default refusal, explicit one-filter acceptance, forbidden mixed changes,
  forged/unknown profile refusal, filter failure rollback, idempotent recovery
  and unchanged unrelated process/disabled intent. Use copied native recovery
  proof for filter scope where the existing harness can express it.

Codex reviews one focused package. Run the full Python suite after integration;
reuse still-current portal/browser results since this slice does not alter UI.

## Task 3: activate and accept filter slice

Codex alone performs activation. Build the complete 43-owned-file bundle and
verified Android gateway from accepted source. Compare the phone to the previously
deployed 43-file manifest, including permissions and known boot alias; refuse
unexpected drift rather than automatically refreshing expectations.

- Establish the installed GraphQL filter and empty-IN contract with metadata and
  phone-local requests; export only sanitized results, never library content/IDs.
- Preserve pinned DNS source/library/launcher and all disabled markers. Plan must
  show one changed filter destination and only `suwayomi_filter` affected.
- Deploy through the explicit profile and retained transaction, with source and
  control hashes checked before execution. Do not patch installed files directly.
- Verify all file hashes/modes, completed/committed journals, one healthy filter,
  benign GraphQL forwarding, actual cache behavior where safe, unchanged Java and
  unrelated start identities, fresh supervisor state and portal functionality.
  Do not alter category membership or run updates/downloads to test deployment.
- On failure use service-aware rollback. Keep transaction/recovery directory.
  Record evidence and any unavailable live proof precisely; stop re-testing once
  acceptance conditions pass.

## Task 4: consolidate lifecycle ownership and add scheduler

Start from accepted filter slice. Use a fresh bounded worker and worktree. Follow
the approved design: no launcher background forks; supervisor owns backend,
filter and one foreground scheduler; 16 managed services and nine dashboard cards.
Preserve JVM/DNS/PRoot argv and midnight/manual/retry semantics. Scheduler health
uses exact process identity, not battery guard status. Singleton handling must
recognize an existing owned daemon or refuse ambiguity without killing decoys.

Create an explicit lifecycle profile that permits only reviewed Suwayomi changes
and the exact appended scheduler catalog entry. Prior 15 runtime facts and order
stay unchanged. Extend exact matching, quiescence and prior/candidate journal
binding for a service absent from the prior catalog. Candidate scheduler must stop
before old catalog restoration; never try to launch it in the restored prior set.

Use fake time/no real GraphQL mutations for schedule characterization. Prove
foreground singleton lifecycle, disabled markers, intentional stop, dead-process
recovery, no immediate update on startup, copied bundle-free rollback and repeat
recovery for 15-to-16 transition. Review source and meaningful proof before one
full integration gate. Do not weaken the accepted general or filter profile.

## Task 5: activate lifecycle slice and close acceptance

Codex builds a separately reviewed complete bundle against the accepted filter
release. Capture fresh metadata only for known expected changes; refuse unrelated
live drift. Inspect the exact affected service plan, then perform authorized brief
scoped restarts. SSH and unrelated services stay available.

Verify one backend, filter and scheduler; healthy supervisor state; no old Java
child zombies after the scoped backend restart; unchanged JVM/DNS pins and disabled
intent; current native/portal/thumbnail route compatibility. No immediate library
update or database/extension/download modification may be used as a test. Record
new release recovery paths and operational ownership. Preserve old transactions.

DNS replacement, heap tuning, catch-up scheduling and generalized GET feed
rewriting remain outside these releases. Record remaining questions without
quietly changing policy or claiming unmeasured performance improvements.
