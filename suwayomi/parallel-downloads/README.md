# Bounded Suwayomi chapter downloads

This patch targets upstream `v2.4.2366`, commit
`40bc5da57820324ed0ae724cbbc020cc4673b594`, with git revision count 2366.
Retain the upstream MPL-2.0 notices. `source-pin.json` records the reviewed patch
and source fingerprints; `sources/` contains readable copies of the patched files.
Keep those copies, the generated patch and pin manifest synchronized.

## Behavior

One identity-bound claim owner admits at most two chapters per source and six
chapters globally. Claims remain held until cancellation settles. Dequeue captures
the original queue-item/job identity and cannot cancel a replacement instance.
Ready sources receive a first lane before spare second lanes. The existing
`maxSourcesInParallel` remains a source-count setting: one source can still run
two chapters, within the independent six-chapter cap.

A shared six-permit budget covers metadata and full image streaming/closure.
Pages remain serial. Reorder revokes work at a safe checkpoint without charging a
retry; stop settles claims and returns permits. Existing queue persistence, paths,
image clients and archive behavior remain in use.

Concurrent output collision preservation: chapters resolving to the identical
canonical effective output path (from source display name, manga title, scanlator, and chapter name)
are mutually excluded across their full lifecycle (preparation through archive finalization)
via a reference-counted keyed Mutex registry owned by `Downloader`. Waiting chapters hold
neither admission locks nor network permits. Distinct output paths continue in parallel.
Cancellation while waiting safely decrements the reference count and prunes the registry
without leaving orphan path-lock entries or leaking Mutex instances.

## Build inputs and checks

The verified host toolchain is OpenJDK 27.0.1.0.3, Gradle wrapper 9.7.1 and Kotlin
2.4.10, targeting Java 21 classfile major 65. The mutable upstream Android stub
repository requires the authentic `android-jar:1.0.0` at commit
`3983289d9481a8ba99c0b16350b27f41364906a2`, SHA-256
`2743be4f4c9ba50f8c86ccc0769e5be5246542488b34e6085a2574ca8ac303e3`.
The private Gradle cache prepends its checksum-verified local Maven copy using
`init.d/toolchain-pin.init.gradle.kts`. Recreating a cache requires restoring that
input pin; do not fall back to the mutable branch's current stub.

Apply `bounded-chapters.patch` only to the clean pinned checkout. From the patched
checkout, run the existing focused gates with the repository's test-heap init file:

```sh
ProductBuildType=Stable GRADLE_USER_HOME=/path/to/pinned/private-gradle-cache \
  ./gradlew --no-daemon --max-workers=2 -Dorg.gradle.jvmargs=-Xmx2g \
  -I /path/to/mobile-server/suwayomi/parallel-downloads/verification/production-test-heap.init.gradle.kts \
  :server:test --tests suwayomi.tachidesk.SafePathTest \
  --tests suwayomi.tachidesk.manga.impl.PageTest \
  --tests suwayomi.tachidesk.manga.impl.download.BoundedParallelDownloaderTest \
  :server:shadowJar
```

Root's previous focused gate passed 18 tests: four SafePath, one PageTest and thirteen
downloader tests. Two new regression tests bring downloader tests to 15 (20 focused tests total):
verifying output collision serialization and output integrity, and proving cancellation while
waiting cleans up the registry without leaked locks or references. Tests use a 384 MiB heap and production JVM options; the larger
heap in the command belongs to the Gradle build process. Tests gate actual response
streams, exact page bytes and names, cancellation settlement, quick dequeue/requeue, stable-ID deduplication, clear/enqueue settlement,
reorder, retries, source admission and both independent concurrency bounds. The
network test deliberately admits ten claims to pressure six network permits.
Limit overrides exercise serial and unbounded behavior on the real download path.
They are test-only controls, not operator settings.

## Packaging and remaining acceptance

The full compiled shadow JAR is a compilation input, not the phone artifact.
The installed JAR already contains reader, native-library and dependency
customizations. Use the separately reviewed preservation packager with its pinned
installed base. Include all five reviewed class owners explicitly:
`DownloadManager`, `Downloader`, `DownloaderNetworkBudget`, `ChapterClaim`, and
`fileProvider/ChaptersFilesProvider` in the download package.

Verification must load production classes and dependencies from the packaged JAR,
then run paired resource checks. Host compilation and focused tests alone do not
prove phone suitability. Page concurrency stays disabled until its additional
resource gate passes. Deploy only through the separately reviewed JAR-only profile
with a retained prior package and source/health checks. No custom JAR deployment is
claimed by this source change.


The packaged-runtime gate adds `PackagedClassOriginTest`: it checks the origins of
five downloader classes and six dependency classes, then checks the test heap cap.
All 21 tests passed against the local preserved-base candidate. Set
`MOBILE_SERVER_PROOF_ROOT` to the maintained proof checkout and add
`verification/packaged-runtime.init.gradle.kts` to the normal test-heap init file.
The candidate must exist at `.work/packaged-candidate.jar`. Include
`--tests PackagedClassOriginTest` with the three existing focused suites. The init
file adds the Java origin check and places the packaged JAR before the configured
test runtime. It resolves the original classpath lazily in the task context.
This host gate does not replace the paired resource or Android acceptance gates.

## Output-path collisions and acceptance

Upstream derives chapter paths from display names. Distinct IDs can therefore
share one existing output path. A reference-counted mutex in `Downloader`
serializes the complete lifecycle for each canonical output path. Waiting jobs
hold no network permit; unrelated paths remain concurrent. Cancellation releases
the waiting reference, and the final owner removes the registry entry.

Existing serial behavior recognizes an already completed aliased output. This
patch preserves that behavior and existing filenames; it does not migrate paths
or replace a completed alias with another chapter's bytes. The gated regression
checks exclusion while another path completes. Bypassing the mutex while retaining
reference accounting fails that actual preparation/write assertion.

The compiled snapshot and complete patch passed the 20-test focused gate and
21-test preserved-base runtime gate under the configured `-Xmx384m` limit. The
source pin records the verified compilation input. The packaged candidate passed its 16-trial paired resource gate and native phone
activation acceptance. See `docs/SUWAYOMI-PARALLEL-RELEASE.md` for exact artifact,
remaining limits, and retained rollback.

## Conditional Page Concurrency Candidate Status

The conditional two-page-worker slice in `ChaptersFilesProvider.kt` bounds page worker concurrency to at most 2 coroutines per chapter within structured `coroutineScope`. It ensures siblings and progress collectors settle before ComicInfo or archive finalization. The updated packaged candidate (`3dd150705d07486e4f890711a2d01a1a67a35d71a0fdc444cf7d5661473c221e`) passed 29/29 packaged-runtime unit tests and the 24-trial host native paired probe matrix (`org.probe.MainNativeProbe`). It remains local pending root phone paired execution and activation.
