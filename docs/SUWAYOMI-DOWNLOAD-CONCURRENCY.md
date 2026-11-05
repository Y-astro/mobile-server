# Suwayomi download concurrency findings

The user requested parallel server downloads on 2026-11-05. They clarified the
priority: concurrent chapters from the same source, plus cross-source downloading;
add page concurrency too if resource checks support it.

## Verified baseline

At 05:31 UTC, root independently read the installed JAR manifest and GraphQL
settings. The server is `v2.4.2366`, revision `r2366`, with
`maxSourcesInParallel = 6`. The queue was empty and stopped. No setting was changed
and no download was started. An empty queue provides no throughput measurement.

The version-pinned upstream implementation has one downloader per source, takes
one chapter at a time from that source, and awaits page downloads in a sequential
loop. The six-source setting therefore does not enable concurrent chapters from
one source. No supported same-source chapter or page concurrency setting was
found in the inspected schema and implementation.

Primary sources, independently checked by root:

- [DownloadManager.kt](https://github.com/Suwayomi/Suwayomi-Server/blob/v2.4.2366/server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/DownloadManager.kt): one downloader per source and the dynamic source-concurrency bound.
- [Downloader.kt](https://github.com/Suwayomi/Suwayomi-Server/blob/v2.4.2366/server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/Downloader.kt): sequential selection and completion of chapters within a source.
- [ChaptersFilesProvider.kt](https://github.com/Suwayomi/Suwayomi-Server/blob/v2.4.2366/server/src/main/kotlin/suwayomi/tachidesk/manga/impl/download/fileProvider/ChaptersFilesProvider.kt): sequential page retrieval inside a chapter.

The local phone client's application, version and configuration have not been
inspected. Its observed parallelism is not disputed or assumed to use identical
rules. The exact upstream rationale for serial behavior is not established by
these code paths.

## Required next outcome

The requested extra parallelism needs a maintained downloader change, rather than
a fabricated configuration toggle. Preserve the existing six-source bound.
Lowering it to three would reduce configured parallelism and would not solve the
request; no measured thermal problem supports that change.

Investigate a small version-pinned patch with bounded same-source chapter lanes,
fairness between sources, correct cancellation/retry/reorder/progress behavior,
and isolated per-chapter output. Add page workers only behind a shared request
budget and measured acceptance gate. Do not multiply source, chapter and page
limits into unbounded simultaneous work.

Before deploying a custom binary, establish its reproducible build, provenance,
resource limits, fake-source concurrency/output tests and software-only rollback.
Keep the current JAR for backout. Preserve the database schema, library,
extensions, downloads, JVM options and intentional DNS fix. Do not use actual
user downloads as an unannounced load test or promise zero resource impact.

This document records diagnosis and user priorities. Custom parallel downloading
has not been implemented or enabled. Detailed feasibility and implementation
design are in progress independently of the existing lifecycle repair.
