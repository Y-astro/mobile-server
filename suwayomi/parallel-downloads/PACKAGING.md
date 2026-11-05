# Preserve the installed Suwayomi package

The phone's installed v2.4.2366/r2366 JAR is a customized upstream build. Its
SHA-256 is `9b7bf58329eccb3dcfb7246360f6818f7391b8804330a9e0491006049678bedc`.
It contains the category-1 unread reader patch, a local WebUI validation change,
and an Android Bionic zstd JNI library. A newly built upstream shadow JAR contains
a glibc AArch64 library instead. Do not deploy that whole rebuilt JAR to the phone.

The installed archive also has 1,520 duplicate dependency names. Comparing only
the last occurrence of each filename hides dependency differences: the local
baseline and installed archive differ at 1,452 names when every occurrence is
compared. There are no duplicate Suwayomi application entries. Keep every installed
dependency occurrence in its original relative order; do not deduplicate or replace
unrelated classes during this downloader change.

## Packaging boundary

`package-inputs.json` pins the installed packaging base, upstream source commit,
custom entry hashes and exact downloader class owners. The base JAR is a build
input kept outside Git; it contains application code, not user databases or runtime
credentials. Retain the original fingerprinted input and deployment preimage.
Never refresh its pin automatically from whatever JAR happens to be installed.

Build the reviewed source patch against the pinned upstream checkout first.
`package_jar.py` then replaces only the declared downloader classes and their
compiler-generated nested classes. It removes obsolete classes belonging to those
owners and retains every other installed entry's content and order, including
all duplicate dependency occurrences. It retains the customized WebUI, existing
validation behavior, native libraries, translations and database migration classes.
The native library is preserved as a pinned input; this procedure does not claim
to reproduce its original native build from source.

The packager verifies input fingerprints, refuses signed archives requiring a
separate signing workflow, and requires Java 21 class headers for replacement
classes. It writes explicit custom-build manifest attributes and
`META-INF/mobile-server-build.json`, containing upstream, source patch, compiled
artifact and base fingerprints. Application protocol/version constants remain at
the compatible upstream revision. Existing output files are never overwritten.

Run from the maintained home-server repository, substituting reviewed build values:

```sh
python3 suwayomi/parallel-downloads/package_jar.py \
  --base-jar .work/inputs/installed-custom-r2366.jar \
  --compiled-jar /path/to/reviewed-built.jar \
  --compiled-sha256 REVIEWED_COMPILED_SHA256 \
  --source-patch suwayomi/parallel-downloads/bounded-chapters.patch \
  --patch-sha256 REVIEWED_PATCH_SHA256 \
  --source-date-epoch REVIEWED_SOURCE_COMMIT_EPOCH \
  --output .work/suwayomi-parallel.jar
```

The output must pass runtime tests using the packaged JAR's production classes and
dependencies, not only the full compiler output. Check class loading origin during
that proof. Run paired resource checks with the production 384 MiB heap before
retaining extra concurrency. Page workers remain serial until their separate gate
passes. Neither packaging checks nor Java class headers alone prove downloader
correctness, Android JNI loading, or resource suitability.

Use the separately reviewed JAR-only activation profile and retained software
preimage for deployment/backout. Do not copy a replacement over a running backend.


The reviewed downloader correction introduces a top-level `ChapterClaim` data
class. It is explicitly included as the fifth owner in `package-inputs.json`.
The local candidate built with this exact five-owner configuration passed the
19-test packaged-runtime gate, including the new class's loading origin. This
configuration update does not broaden selection to arbitrary package classes.
