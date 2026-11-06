# Cloud storage and glass appearance design

Status: approved by the operator on 2026-11-05 with “continue” after review of the full proposal and the implementation/deployment approval question. This document records that approved scope.

## Scope and alternatives

Recommended: retain the current FileBrowser backend, gateway, permissions and vendored frontend. Add source-owned complete styling, a small verified entry-module loader and one shared appearance controller. A frontend fork needs ongoing upstream build and merge work. Replacing the file manager introduces migration and file-operation scope beyond this request. YouTube downloading and media/torrent remain explicitly on hold.

## Visual direction

- Product: the operator's private Redmi server for managing services and files.
- Character: calm, tactile, precise.
- World: the operator's wallpaper behind quiet graphite surfaces.
- Moments: the selected wallpaper and real file previews carry color; chrome remains restrained.
- Signature: the same Appearance control and teal selection treatment across the portal and cloud.

Reuse the existing dark Material3 typography, spacing, shapes and colors: surface#111414, container#1d2223, teal#70d6c5 and gold#e5c46c. Restyle the full cloud interface: navigation, breadcrumbs/search, list/grid, selected files, action/context menus, dialogs, upload progress, preview and editor surfaces. Preserve file capabilities, code syntax readability and current keyboard/role behavior. Use responsive layouts and accessible targets/focus; no new frontend framework, font dependency or generic visual effect suite.

## Appearance behavior

Both main authenticated portal pages and cloud storage expose a consistently labeled Appearance control. The panel provides a lazy wallpaper gallery, preview, wallpaper selection, blur0–100%, translucency0–100%, and Reset. Blur maps linearly0–30px; default50% means15px. Translucency means panel background transparency:0% opaque,100% clear; default35%. Text/icons stay opaque. A separate wallpaper readability tint and readable content treatments must maintain contrast at slider extremes; do not silently remap100% to a different transparency. Keep image/file previews unblurred. Do not animate blur on scroll or apply repeated nested backdrop filters to every file row. Reduced-transparency preference uses solid surfaces; missing images or unsupported filters degrade to the existing M3 surface.

One validated browser preference object persists across reloads and both paths on the same origin. No cross-device/account synchronization was requested. Handle unavailable/corrupt storage with defaults and a usable panel. Image selection uses known manifest IDs, not arbitrary URLs. Use safe DOM/text APIs for filenames. Reset restores defaults and the shipped initial selection. Choose a suitable default from the supplied images during visual verification; expose every supplied image in the picker. Settings should be clearly described as saved in this browser.

## Wallpapers and access

Copy all26originalpictures,296,214,309bytes total, exactly into a new ~/wallpapers directory on the phone. Preserve original bytes/names and verify every SHA-256. Exclude the three videos. Originals, generated images and local source inventory stay out of Git. Separate browser display and thumbnail derivatives from originals. Preserve image aspect ratio for display derivatives (bounded long edge around2560px), and use lightweight lazy thumbnails for the gallery. Keep gallery/image access authenticated; before login or on401, show the solid/built-in safe fallback rather than exposing private user images.

The data importer is idempotent: same hashes may be reused, differing existing files are not overwritten, and staging is verified before activation. The new directory is currently absent and its parent is writable by TermuxUID10505. Do not change FileBrowser account scopes or filesystem permissions. The new authenticated library manifest/image handler serves only known generated assets, refuses traversal and returns missing-image fallbacks. Original pictures remain after a UI rollback because the user explicitly requested their upload.

## Integration boundaries

Existing native custom.css loading is verified. Use an explicit Caddy route to a source-owned stylesheet while preserving the prior unowned branding/custom.css as rollback fallback. FileBrowser does not natively load its existing custom.js file; that URL returns404. Load the shared browser controller through a tiny source-owned ESmodule entry wrapper which imports appearance.js and reexports the existing bundle under another filename in the same assets directory. There are29named exports and no default export. Same-directory aliasing preserves relative Editor/Preview chunks. Portal templates load the same controller directly. Keep gateway and vendored bundle unchanged if actual-app acceptance verifies the wrapper. Do not claim source inspection proves runtime compatibility.

Share the existing theme tokens rather than duplicating diverging palettes. Bound any extraction to current theme values/comments and real consumers. Extend explicit asset ownership and reviewed release manifests; refuse unexpected FileBrowser version/entry-hash drift. Keep existing authentication, transfer streaming and service intent. No direct ad-hoc live code changes. Use source-first controlled deployment with reviewed file hashes and rollback.

## Acceptance and deployment

Verify the actual FileBrowser app mount, lazy preview/editor modules, named imports and single initialization with zero module errors. Exercise real UI inputs for wallpaper selection, both sliders, Reset, reload and navigation across portal/cloud; use both desktop/mobile layouts. Verify opaque text, slider-extreme readability, reduced-transparency fallback, missing-image/storage/auth failures and permission boundaries. Preserve upload/download/rename/delete/search/list/grid behavior using isolated synthetic fixtures, never destructive tests on real user files. Root reviews screenshots and decisive test evidence from AGY implementation workers.

Confirm all26phone original hashes and authenticated thumbnail/display responses. Validate complete release ownership and drift checks; deploy only affected services with retained recovery. Preserve the live Suwayomi3dd release, intentional DNS shim and current disabled flags. No promise of zero downtime or unmeasured performance gains. No change to the held YouTube/media/torrent tasks.

## Approved role correction, 2026-11-06

The user explicitly restricted Appearance choices to administrator accounts. Remove the family and login triggers. The shared portal/cloud controller must confirm the existing authenticated admin check before mounting controls or opening the dialog, including direct JavaScript invocation. Treat redirects, denial, server errors, and timeouts as refusal. Family users retain the existing Material 3 styling and authenticated wallpaper rendering. Preferences remain per browser and origin; no global settings or new backend endpoint is introduced. Verify actual admin controls, family portal/cloud refusal, anonymous redirect refusal, and unaffected native file actions. Deploy through the existing complete-manifest general profile.
