# Recover missing MangaFire library covers

This repairs missing files in Suwayomi's existing thumbnail cache. It does not
change manga records, source assignments, read progress, downloads, JARs, routes,
or service state. Existing thumbnail paths in every recognized format are kept.
It is a data repair, not a replacement MangaFire extension: newly added titles or
cleared cache entries may need another collection until source support changes.

The publisher also accepts reviewed Vortex Scans JPEGs from that source's exact
`storage.vortexscans.org/upload/series/featured/` path. Vortex assets require a
separate source manifest and first-party page proof; the MangaFire collector does
not collect Vortex titles. Source IDs and CDN hosts cannot be mixed.

The host collector reads only in-library source metadata and current cache names
from the Redmi over SSH. Normal Chromium page navigation supplies the site's own
metadata request; no verification token is generated or reverse engineered. A
challenge stops collection. Canonical image URLs come from the matching title's
API response, with exact HTTPS CDN-host validation. Existing Playwright and host
Pillow fully decode the bounded JPEG payloads. No additional packages are installed.

```sh
node tools/collect_mangafire_covers.cjs --dry-run
node tools/collect_mangafire_covers.cjs --limit 128 --staging-dir .work/NEW_PRIVATE_DIR
```

The output directory must be new. Its manifest and images are private and must
not be committed. Check collection failures before publication: a partial batch
contains only successfully decoded images. HTTP404 title metadata cannot supply
a canonical cover. Do not guess CDN prefixes or use another title's image.

Review the source commit and stage/verify the tool, manifest, and every image hash
in a private directory on the phone. Run the publisher tests in a disposable phone
directory first. The publisher uses only Python's standard library and defaults
to dry-run:

```sh
python tools/publish_suwayomi_covers.py --staging-dir PRIVATE_STAGE --json
python tools/publish_suwayomi_covers.py --staging-dir PRIVATE_STAGE --apply --json
```

Publication verifies IDs, paths, exact source identity, file inventory, dimensions,
size caps, hashes, and JPEG signatures. It holds verified bytes in memory, pins the
target directory with a descriptor, and writes only complete fsynced hidden files.
Linux/Android `renameat2(RENAME_NOREPLACE)` exposes each completed thumbnail without
clobbering a concurrent Java write. Unsupported atomic operations fail closed.
Temporary cleanup checks file identity, so pre-existing collision files are kept.
Per-file publication is idempotent; interrupted batches can leave valid published
covers, and another run skips those files. No whole-cache rollback or purge occurs.

Acceptance requires all pre-existing cache entries unchanged, published bytes
matching the reviewed manifest, real native and authenticated portal image
responses, category switching, and preserved server process/service state.

Focused checks:

```sh
python3 -m unittest tests/test_suwayomi_cover_publication.py -q
node --test tests/cover_collector.cjs
```
