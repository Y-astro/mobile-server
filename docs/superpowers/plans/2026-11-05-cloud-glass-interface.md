# Wallpaper appearance interface

Approved design: docs/superpowers/specs/2026-11-05-cloud-glass-design.md. This contract fixes the boundary between independent UI and data owners. Do not add account settings, API mutations, extra services or frameworks.

## HTTP

GET /api/wallpapers (authenticated, admin or family) returns:

```json
{"version":1,"default_id":"<64 lowercase hex>","wallpapers":[{"id":"<64 lowercase hex>","name":"Original filename.png","width":3840,"height":2160,"thumbnail_url":"/api/wallpapers/<id>/thumbnail.webp","image_url":"/api/wallpapers/<id>/display.webp"}]}
```

Missing/not-yet-imported library returns the same shape with default_id:null and wallpapers:[] (200). Corrupt/incomplete library is503 with the existing JSON error shape, without filesystem paths. Unknown images/path traversal return404. Unauthenticated list AND images return401 before reading data. Both image routes are read-only image/webp responses; only manifest-listed64hex IDs and the two exact derivative basenames are accepted. Use private,no-store cache policy for library and images. URL parameters never become unrestricted filesystem paths. No originals are served through this API. Session auth remains existing Handler.get_session_user; no invented separate cookie/token.

## Filesystem

Phone library root: $HOME/wallpapers. Host build root: ignored .work/wallpapers-library. Layout: manifest.json, originals/<exact source filename>, display/<id>.webp, thumbnails/<id>.webp. ID=SHA-256 of original bytes. Build all26images from /home/astro/Pictures/Wallpapers; exclude3videos. Preserve names/bytes; never write originals or images toGit. Manifest records original/derivative relative paths, hashes, bytes and dimensions for import verification. Public list excludes private/internal file records and retains ONLY the response fields above. Backend derives allowed derivative paths from validated IDs and fixed directory/basename policy, rejecting symlinks/escapes. Default is06. Frosted Glass.png when present, otherwise deterministic first filename. No extra Python runtime dependency; Pillow is already installed on HOST and allowed only in the offline build tool. Process one image at a time; respect EXIF orientation in derivatives, preserve display aspect ratio, long edge<=2560 without upscaling; thumbnail<=320x180 using contain or intentional cover, no stretching. No invalid JSON/non-finite values.

Offline builder/importer goes in tools/prepare_wallpapers.py (avoid accidental script deployment). It validates all inputs before final publication, reuses identical verified builds or refuses conflicting destinations, writes crash-safe staging, and emits a strict complete transport manifest. Expose a standard-library-only verification/publication mode for root's phone staging; existing differing user files must never be overwritten. Root alone transfers/publicizes the data after review. No worker SSH writes or actual upload.

## Shared UI assets

Paths served by Caddy AND dashboard's exact public asset allowlist: /theme-tokens.css, /appearance.css, /appearance.js. UI-owned cloud assets: web/filebrowser.css and web/filebrowser-entry.js. The native entry wrapper imports /appearance.js and reexports all names from ./mobile-filebrowser.js; Caddy serves existing web/filebrowser.js at that same-directory alias. Native /drive/static/custom.css gets an exact Caddy source-owned override; preserve prior branding/custom.css unchanged. Root owns Caddy and deployment mappings, not either implementation worker.

web/appearance.js must be valid both as a classic deferred script on the actual portal templates and as a side-effect module imported by the cloud entry. Initialize once. Use one localStorage key mobile.appearance.v1, object {version:1,wallpaperId:<id or null>,blur:50,translucency:35}; validate percentages as finite0..100 and ID against fetched manifest; handle all storage failures. Persist per browser across same-origin portal/cloud only. Defaults blur50%=15px (max30px),translucency35%;0%=opaque panel,100%=clear panel. Text/icons stay fully opaque. Do not silently clamp/remap translucency to another percentage. Keep a separate stable dark wallpaper tint/readable content treatment for contrast, including at100%; opaque fallback with reduced transparency or unavailable backdrop filters. No wallpaper API needed on every slider input; one bounded fetch with explicit failure/retry behavior, no polling. Lazy thumbnails and safe textContent/URL use. User-facing preference copy says saved in this browser.

API owner also adds exact public shared-asset GET handling in app/dashboard.py, matching existing portal.css conventions; no directory/file arbitrary serving. UI owner does not edit app/dashboard.py. The dashboard fixture can serve the same public paths directly.
