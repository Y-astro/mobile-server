# Library query row loading

The website requests `mangas.nodes.chapters.totalCount`. The original resolver loads
and constructs every chapter before returning that count. This patch uses the
existing request-scoped chapter-statistics loader and adds `COUNT(id)` to its SQL
aggregate. Requests for nodes, edges, page information, or other fields retain the
original chapter-list loader. Aliases and fragments use GraphQL Java's resolved
selection set. No persistent cache, schema change, or database migration is added.

## Source and build

`source-pin.json` identifies the upstream commit, original source hashes, current
custom base JAR, and nine replaceable class owners. Apply `library-count.patch`
to a clean checkout of that upstream commit after checking those source hashes.
Do not modify a shared build checkout. The patch changes only `MangaType.kt` and
`ChapterDataLoader.kt`. Six chapter selectors also use SQL `DISTINCT ON (manga)`
to return the first matching row per manga, retaining their existing predicates
and within-manga ordering. Both H2 and PostgreSQL support this operation.

Build the patched project using its pinned Kotlin 2.4.10 compiler and JVM target
21. Alternatively, compile those two files against the pinned custom JAR and its
existing dependencies, using `-Xfriend-paths=<base.jar>` for the upstream internal
`toGenreList` extension. Keep the compiler version fixed. Do not upgrade dependencies.

Compile `tests/LibraryCountProof.kt` against that same dependency classpath. Run
`LibraryCountProofKt` in an isolated working directory and home, with patched
classes first on the classpath and `-Xmx384m`. It creates only a fictional,
in-memory H2 database. The original build must fail with
`Count-only selection loaded full chapter rows`; the patched build must pass all
17 GraphQL cases plus six selector parity cases. They cover empty and all-read manga, batched counts, existing flags,
aliases, fragments, conditional fields, mixed count/full selections, pagination,
fresh requests, and mutation cache invalidation. Selector checks compare with
the original full-row algorithm, including tied dates/orders, missing manga,
empty batches, read/unread filters, and non-positive chapter numbers. The H2 fixture explicitly permits
upstream unlimited VARCHAR lengths, matching the existing column model.

Package only the reviewed owners:

```sh
python3 suwayomi/library-query/package_jar.py \
  --base /path/to/pinned-current.jar \
  --classes /path/to/compiled/classes \
  --output /path/to/new-candidate.jar
```

The packager preserves every other archive occurrence, including duplicate
resources, the original manifest, Android shims, downloader customizations, read
cleanup, and previous build provenance. It adds separate library-query provenance.
Run the same Kotlin proof with the packaged JAR first, without loose patched
classes, and compare hashes of every untouched archive occurrence. Python packaging
regressions run with `python3 -m unittest discover -s tests -p test_suwayomi_library_package.py`.

## Phone acceptance

Use the existing reviewed `suwayomi-jar` activation profile and complete release
manifest. The included activation change accepts the exact current sidebar catalog
hash; its 18 runtime service recipes were verified identical to the prior reviewed
Navidrome catalog. Unknown hashes and changed service counts remain rejected.

Capture the real website request before and after activation, with the same data,
warmup, and repeated samples. Require identical parsed responses and at least 20%
lower median server latency before retaining the candidate. Preserve disabled
service flags, DNS shim, launch arguments, and unrelated process identities. Keep
the old JAR and transaction recovery assets for rollback. Do not infer full client
speed or download throughput from an idle API check. This patch does not repair
failed source cover hosts or alter Tsumiru update-feed filtering.
