# Suwayomi reliability design

Status: Approved by the user's instruction to continue on 2026-11-04, after review
of the staged design. General-server stabilization remains deployed. Implementation,
verification and the scoped releases described below are authorized; routine steps
within this design do not require another approval.

## Goal and preserved behavior

Make the custom Suwayomi integration independently maintainable and restore
monitoring of its daily updater. Preserve the library, downloads, extensions,
database, native-client access policy, ports 4569/4566/4567, portal routes,
thumbnail routes, Category 1 Reading feed intent and existing JVM options.
Preserve the intentional DNS preload and PRoot working-directory fix.

## Verified starting point

At 2026-11-04 15:50 UTC, root independently verified that Java and the filter were
running, their endpoints returned 200, and no scheduler process existed. The
recorded scheduler PID no longer existed. Its last saved run was successful on
2026-11-04; that historical status does not establish future scheduler coverage.
Two defunct Python children remained under Java. Their exact exit causes were
not established.

Both launcher and supervisor contain filter startup logic. Historical bind
conflicts support investigating this overlap, but counts alone do not prove the
precise cause of each conflict. Scheduler startup belongs only to the launcher;
the supervisor cannot currently recover a dead scheduler.

Root's isolated execution of the maintained cache functions reproduced three
upstream fetches for three immediate reads of a valid empty category. A null
category returns previously cached IDs through the caught exception path; it
does not crash the server. The recognized update-feed POST is forwarded without
restrictions when IDs are empty. GET forwarding also changes upstream HTTP
errors into 502 responses. These are source-level defects; the live user's
category was not emptied to reproduce them.

The existing DNS library is mapped into Java, and launcher/source/library hashes
match the intentional captured versions. Its C source reads before a foreign
`addrinfo` allocation when testing its ownership marker. That is a source safety
finding, not proof of a live crash or proof that the installed binary matches the
source. Resolver-path correctness and representative JVM heap use remain
unproven. No tuning or DNS replacement follows from the idle RSS sample.

## Approaches

1. **Staged integration repair — recommended.** Correct the filter in one release,
   then consolidate process ownership in a second release. Each has its own
   regression proof and backout. Java need not restart for the filter release.
2. **Combined integration release.** Repair filter and ownership together. This
   reduces activation steps but couples request changes, a new managed service
   and backend restart into one acceptance decision.

DNS replacement and JVM tuning are separate investigations. Mixing either into
these repairs would add unmeasured behavior and weaken diagnosis.

## Slice 1: filter correctness and cache ownership

Keep one filter process on loopback port 4566. Put runtime setup behind the main
entry point so importing request/cache decisions does not create directories,
open logs, start threads or contact the backend. Separate pure transformations
from HTTP forwarding without adding a framework or third-party dependency.

Represent cache validity separately from its IDs. A successful empty category is
a valid cached result for the existing 30-second TTL. Concurrent readers share a
single refresh. Preserve the previous successful snapshot on transient refresh
failure; if no valid snapshot has ever existed, return a controlled retryable
error for a recognized feed instead of forwarding it unrestricted. A successful
missing/null category represents no Reading entries. Do not cache malformed or
GraphQL-error responses as successful emptiness.

For recognized update-feed POSTs, apply the Reading, unread and in-library
constraints even when the valid ID list is empty. Confirm the backend's empty-IN
semantics before acceptance. Keep non-feed queries and mutations unchanged.
Validate the expected request object/variables/filter shapes; malformed matching
requests receive controlled errors rather than silently bypassing restrictions.

Forward upstream HTTP error status, body and relevant headers consistently for
GET and POST. Remove conflicting length, connection and generated server/date
headers; set the final content length once. Retain existing HTTP version unless
a demonstrated keepalive need justifies a separate tested change. Use JSON
encoding for generated error bodies. Bound refresh attempts and preserve the
existing cache interval; an empty category must not create a request storm.

Do not broaden Caddy's GraphQL matcher or generalize arbitrary GraphQL operations
in this slice. GET feed compatibility and query recognition need a separate
contract check before changing native-client behavior.

## Slice 2: one owner for each process

Make the supervisor the sole owner of backend, filter and scheduler. Remove
background filter/scheduler starts from the Java launcher while keeping its DNS
preload, working directory, Java argv and PRoot fallback. The existing daemon
interface remains available for explicit manual use, but normal supervision runs
the scheduler with its existing `--foreground` interface.

Add one managed `suwayomi_scheduler` entry, giving 16 managed services and the
same nine dashboard cards. Match scheduler processes by owned script argv and
foreground/daemon mode, UID and process start identity. Scheduler health must use
its own process state, not the battery guard's heartbeat file. Observe an existing
instance and its singleton lock before starting another; refuse ambiguous state.

Preserve the current local-midnight IST trigger, Category 1 target, retry limits,
status format and explicit `--now` behavior. Starting or restarting the scheduler
must not trigger an immediate update during deployment. Automatic catch-up for a
missed midnight is a later scheduling-policy choice; this slice restores process
recovery without changing when downloads may be queued.

This slice needs a brief backend restart to clear the old launcher ownership and
its zombie children. Verify filter readiness and one scheduler instance afterward.
Preserve existing disabled markers and give the new service its own disable
marker. Do not add a new dashboard control or implicitly enable disabled services.

## Release and rollback contract

The current activation adapter intentionally refuses Suwayomi changes and any
runtime catalog transition. Extend it through an explicit, narrowly validated
Suwayomi release profile; keep the general profile's existing protections.

Slice 1 may select only the filter and necessary reviewed control code. Slice 2
permits the precise scheduler addition and owned Suwayomi launcher/scheduler
changes. All prior 15 service definitions, order and runtime facts stay unchanged;
only the reviewed scheduler entry is appended. Refuse other catalog differences.
Dashboard restarts only if its imported catalog must reload; record the actual
scope in the plan before activation.

Bind the transition, previous service presence, process identities, disabled
intent and recovery assets into the transaction journal. Quiesce a newly added
candidate scheduler before restoring the old catalog. Recovery must not try to
launch a service absent from the prior catalog. A repeated recovery must be safe.
Keep explicit source/preimage checks; never refresh away unexpected phone edits.
SSH remains available. Do not roll back a database or scheduler run history.

## Acceptance and stop conditions

- Use isolated fake upstreams to prove nonempty, empty, missing and failed category
  lookup behavior, cache single-flight/TTL, malformed request handling, unchanged
  non-feed payloads and exact upstream error status/body forwarding.
- Verify recognized empty-IN behavior against the installed backend without
  changing category membership or exporting user IDs/content.
- Characterize existing midnight/manual/retry behavior with fake time and no real
  update mutations. Verify foreground singleton startup, stop and dead-process
  recovery using harmless temporary processes.
- Prove exact process matching rejects decoys and PID reuse. Test disabled intent,
  refusal of unapproved catalog changes and the explicit 15-to-16 transition.
- Exercise copied standalone recovery after bundle deletion for the new-service
  transition. Prove the candidate scheduler stops before prior files return and
  that the old service set and flags are restored. Test repeated recovery.
- On the phone, verify endpoint and benign GraphQL compatibility, fresh supervisor
  status, one filter and one scheduler, unchanged DNS pins/JVM options and unrelated
  process identities. Do not run a library update to prove deployment success.
- Keep existing general-server and portal regression checks passing. Stop after
  both slices meet these conditions; do not add unrelated UI, tuning or rewrites.

## Deferred investigation

Reproduce the captured DNS source in an isolated build and test allocation
ownership, IPv4/IPv6 and fallback behavior before proposing any library release.
No packet trace or JVM attach is required to complete these integration repairs.
Investigate missed-midnight catch-up policy, GET update-feed compatibility,
thumbnail cache semantics and supported application backup/restore separately.
