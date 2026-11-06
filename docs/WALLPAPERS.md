# Wallpaper Preparation & Verification Tooling

The wallpaper subsystem provides an offline preparation tool, standard-library integrity verifier, and atomic publisher.

## CLI Usage

### 1. Build (Offline Host Builder)
Requires Pillow (installed on host). Scans source directory, excludes video formats, verifies against inventory if provided, and generates WebP display (`<=2560px` long edge) and thumbnail (`<=320x180` contain) derivatives while preserving original image bytes.

```bash
python3 tools/prepare_wallpapers.py build [--src <DIR>] [--dest <DIR>] [--inventory <PATH>]
```

Default paths:
- `--src`: `/home/astro/Pictures/Wallpapers`
- `--dest`: `.work/wallpapers-library`
- `--inventory`: `/home/astro/repos/mobile-server/.superpowers/sdd/wallpaper-originals-inventory.json`

### 2. Verify (Standard-Library Only)
Does not require Pillow. Suitable for running on phone or staging environments. Verifies manifest schema, file presence, byte lengths, and SHA-256 hashes for all originals and derivatives.

```bash
python3 tools/prepare_wallpapers.py verify [DIR]
```

### 3. Publish (Standard-Library Only)
Atomically publishes a verified staging directory to the destination (default `~/wallpapers`). Refuses to overwrite destinations with conflicting or differing files.

```bash
python3 tools/prepare_wallpapers.py publish <STAGING_DIR> [TARGET_DIR]
```

## Phone Deployment Workflow for Root

1. Transfer staged files to phone staging:
   ```bash
   rsync -avz --checksum -e 'ssh -p 8022' .work/wallpapers-library/ u0_a505@100.75.159.53:wallpapers-staging/
   ```
2. Copy reviewed `tools/prepare_wallpapers.py` tool to phone:
   ```bash
   scp -P 8022 tools/prepare_wallpapers.py u0_a505@100.75.159.53:prepare_wallpapers.py
   ```
3. Verify staging on phone:
   ```bash
   ssh -p 8022 u0_a505@100.75.159.53 "python3 prepare_wallpapers.py verify ~/wallpapers-staging"
   ```
4. Publish on phone:
   ```bash
   ssh -p 8022 u0_a505@100.75.159.53 "python3 prepare_wallpapers.py publish ~/wallpapers-staging ~/wallpapers"
   ```

## Using Appearance

Open **Appearance** from the main website or the cloud-storage header. Select a wallpaper, then adjust **Blur** and **Translucency**. Changes preview immediately and are saved in this browser for both pages on the same origin. HTTP and HTTPS, different ports, and other browsers have separate preferences.

Blur ranges from 0 to 30 pixels; its default is 50% (15 pixels). Translucency ranges from 0% opaque to 100% clear; its default is 35%. Text remains opaque. Reset restores these values and the default wallpaper, `06. Frosted Glass.png`. A separate dark tint maintains readability. Browsers with reduced transparency enabled, or without backdrop-filter support, use solid surfaces.

Sign in to see the private wallpaper gallery. The API serves generated WebP previews and display images; it does not serve originals. All 26 original images remain byte-for-byte in `~/wallpapers/originals/`. Three MP4 files in the source folder were excluded.

## Verified phone release

Release `cloud-glass-13111981-20261106T024144Z` activated source commit `131119817735aebb291e429a217f25570a0f3614` on 2026-11-06. The general activation profile changed 12 destinations and restarted only dashboard and Caddy. All 49 owned files passed hash, size, mode, and owner verification. The supervisor and all other managed process identities stayed unchanged; 12 services were healthy and four intentionally disabled. Suwayomi's JAR and DNS shim were preserved.

The private library contains 80 files: 26 originals (296,214,309 bytes), 52 WebP derivatives, and two manifests. Both portal listeners passed authenticated admin/family catalog and page checks, unauthenticated refusal checks, and 104 derivative-response hash checks. No user token or signing key left the phone.

Validation: 415 Python tests passed with two skips; the complete existing browser suite and seven cloud-appearance groups passed. Cloud checks use the actual native FileBrowser application with synthetic API data, including folder dialogs, editor opening, lazy module imports, wallpaper selection, both sliders, reset, reload, cross-page persistence, mobile layout, reduced transparency, failed images, and catalog timeout/retry. They require the pinned public assets in `.work/filebrowser-static`; run `npm run test:cloud` after capturing those assets. The normal `npm test` suite does not require this cache. Live checks did not modify or delete user files.

The native integration was verified against FileBrowser `v2.63.23/e8a388f8` and entry `index-BqkXVoMb.js`. Before any FileBrowser upgrade, recheck the entry wrapper, named exports, and relative editor/preview chunks. A new upstream filename does not automatically fail closed; deployment must explicitly refuse unreviewed version/entry drift.

## Rollback

The retained transaction contains independent recovery assets and preimages. Use its reviewed lifecycle controller to roll back both files and affected services:

```bash
ssh -p 8022 u0_a505@100.75.159.53 'python3 /data/data/com.termux/files/home/.cache/mobile-server-cloud-glass-13111981-20261106T024144Z/transaction/recovery_lifecycle/release_activation.py rollback --transaction-dir /data/data/com.termux/files/home/.cache/mobile-server-cloud-glass-13111981-20261106T024144Z/transaction --namespace-root /data/data/com.termux/files'
```

For an interrupted activation, use the same command with `recover` in place of `rollback`. Do not remove the transaction or edit its journals. The uploaded wallpaper library intentionally remains after UI rollback. Existing unowned FileBrowser branding files were preserved.
