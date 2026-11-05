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
   rsync -avz --checksum .work/wallpapers-library/ u0_a505@100.75.159.53:wallpapers-staging/
   ```
2. Verify staging on phone:
   ```bash
   ssh u0_a505@100.75.159.53 "python3 tools/prepare_wallpapers.py verify ~/wallpapers-staging"
   ```
3. Publish on phone:
   ```bash
   ssh u0_a505@100.75.159.53 "python3 tools/prepare_wallpapers.py publish ~/wallpapers-staging ~/wallpapers"
   ```
