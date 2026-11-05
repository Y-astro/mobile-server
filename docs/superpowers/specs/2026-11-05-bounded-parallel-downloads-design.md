# Bounded parallel Suwayomi downloads

## Authorization and outcome

On 2026-11-05 the user requested server parallel downloading, then selected both
chapter and page parallelism if resources permit. Their explicit priority is
same-source chapter downloading plus cross-source downloading. They were told
this requires a downloader code change. The implementation below follows that
authorized outcome; page concurrency is conditional, not assumed safe.

## Baseline and approaches

The installed server is v2.4.2366/r2366. Root verified that six-source concurrency
is already configured and that the queue was empty during inspection. The pinned
upstream implementation processes one chapter per source and pages serially.
Its official source commit is `40bc5da57820324ed0ae724cbbc020cc4673b594`.

Changing the existing source-concurrency setting cannot implement the requested
same-source behavior. A version-pinned downloader patch is the selected approach.
Enabling both levels immediately without resource evidence would violate the
user's condition; chapter concurrency comes first, pages pass a separate gate.
Do not reduce the existing six-source setting to create the appearance of tuning.

## Initial concurrency policy

- At most two active chapters from one source.
- At most six active chapters globally, while preserving capacity for six distinct
  sources. Admit one lane per ready source before spare second lanes; new sources
  must not starve behind spare lanes from already active sources.
- A shared budget of at most six downloader network work units. Cover chapter
  metadata preparation and complete page transfers, including response streaming
  and closure. Do not hold a permit while waiting for child work that needs one.
- Initially one page worker per chapter. Evaluate two page workers only after
  the chapter implementation passes correctness and resource checks, under the
  same global budget. If evidence is unavailable or unfavorable, keep pages serial.

These are conservative starting bounds under the user's resource constraint.
They are not a promise of zero overhead or a measured speed improvement. Preserve
extension HTTP clients, rate limits, queue persistence and the six-source setting.
Do not add new configuration modes or an operator UI unless acceptance requires it.

## Ownership and correctness

Use one clear owner for chapter claims and global admission. Never allow two
workers to claim the same mutable queue item. Use stable chapter IDs for claims.
Keep worker counts bounded; do not launch a coroutine for every queued chapter.
Separate short state changes from network waits to avoid lock/permit deadlocks.

Preserve stop, pause, dequeue, retry, reorder and resume contracts. Reordering may
revoke a spare lane at a safe checkpoint without marking an incomplete chapter
finished or increasing its retry count. Removing one chapter must not cancel a
different chapter. Stop must release all permits and settle all workers.

Preserve existing manga/chapter and page names. Preserve existing partial
cache behavior; do not delete previous completed downloads to simplify cleanup.
Finalize archives and hashes only after all required page work has settled.
Concurrent progress must reflect completed and in-flight work without double
counting, invalid fractions or final success after a failed page.

Upstream builds output and cache paths from sanitized display names, rather than
stable IDs. Different chapter IDs can therefore share a path. Serialize those
collisions across preparation, provider selection, writing, finalization, and
completion; retain parallel work for distinct paths. Use cancellation-safe path
ownership without holding an admission lock or network permit while waiting.
Do not change filenames or migrate existing downloads to solve this collision.
Verify real shared-path exclusion, unrelated-path overlap, waiting cancellation,
and ownership cleanup before building the deployable artifact.

## Maintained source and build

Keep the current upstream version and database schema. Track a small patch set,
pin file and build/provenance instructions in the home-server repository; keep
the full upstream checkout and large artifacts in ignored work directories.
Retain upstream license notices. Do not fork unrelated server, UI or extension code.

First prove an unmodified baseline build. Root verified the source tag; the host
currently has Java 27 only. Determine the pinned wrapper/toolchain requirements
rather than assuming compatibility. A private, checksum-verified JDK cache is
allowed if needed; do not change system Java or install tools on the phone.
Record exact toolchain, dependency/build inputs, source and patch hashes, artifact
hashes, class target and manifest. Embedded build timestamps mean that an identical
build procedure alone does not prove byte-identical reproducibility.

The patched artifact must keep the supported Java 21 target and application schema.
Verify migration resources and relevant settings contracts against the baseline.
Record custom provenance without silently presenting an unmodified official JAR.
Do not claim that the existing installed JAR is reproducible until actually proven.

### Preservation of the installed package

Artifact inspection found existing reader filtering, local WebUI validation and
Android Bionic JNI customizations in the installed upstream-based JAR. It also
found duplicate dependency entries whose contents differ from a fresh build.
Package the reviewed downloader class owners into the fingerprinted installed
base, preserving every other entry occurrence and its relative order. Record
explicit custom-build provenance. Do not replace the existing UI, native library
or dependency contents with a newly built upstream shadow JAR. Runtime acceptance
must exercise the resulting packaged artifact and its retained dependencies.

## Proof and resource gate

Reuse upstream tests where possible, then add isolated fake-source tests that
exercise the real modified downloader path. Require observed overlap, not merely
multiple scheduled jobs. Prove per-source/global limits, source admission fairness,
retry/error handling, cancellation, reorder/resume and exact output integrity.
The tests must fail against a serial or unbounded mutant where practical.

Compare baseline and candidate under the same bounded workload and JVM options.
Measure active requests, completion time, CPU, peak memory, thread/file-descriptor
settlement and failures. Synthetic throughput is not real-source throughput.
Do not raise the existing production heap or change DNS to make tests pass.

Before page concurrency is retained, seek paired evidence on the phone using an
isolated companion harness and temporary data, without starting a second production
backend or touching the user's library/queue. Its process must not match the
existing supervisor's backend selector. Keep workload, duration and resource use
bounded; stop if production health degrades. If a safe representative harness is
unavailable, defer page concurrency and state the limitation.

## Deployment and rollback

Complete the existing lifecycle repair before changing the JAR. Keep the current
JAR and its fingerprint on the phone for software-only backout. Check actual file
kind, permissions, free space and all reviewed live preconditions before packaging.

Extend explicit release ownership for this one artifact only. Preserve the frozen
historical baseline and existing profile protections. Use a complete verified
bundle and a narrow JAR-only activation profile that quiesces the backend before
replacement and checks health afterward. Recovery must stop candidate Java before
restoring the prior JAR and restart the prior software coherently. Do not use a
direct in-place copy while Java runs.

Preserve JVM options, intentional DNS library, root directory, database, extensions,
download paths, disabled intent and native/portal access policy. No database or
library rollback, unannounced real-source downloads or mass queue changes are part
of acceptance. SSH and unrelated services remain available.

## Stop condition

Deliver bounded same-source chapter parallelism with cross-source support, a
verified build, meaningful tests and coherent backout. Retain page concurrency only
if its additional resource gate passes. Report omitted pages or remaining build/
verification limits precisely. Do not expand into an upstream rewrite or unrelated
JVM, DNS, schema, UI or scheduling changes.
