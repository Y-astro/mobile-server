# Phone Server Guardian: Operations & Safeguard Guide

## 1. Overview & Safeguard Role
`phone-server-guardian.sh` runs as Android `shell` UID (2000) in `/data/local/tmp` on the Redmi K20 Pro (`raphaelin`, serial `d7e31d7a`).

Its primary duties are:
1. **MIUI FindDevice Watchdog (every 300s)**:
   - Queries `cmd package list packages -e --user 0 com.xiaomi.finddevice`.
   - If missing or disabled, restores via `cmd package install-existing --user 0 com.xiaomi.finddevice` and enables via `cmd package enable --user 0 com.xiaomi.finddevice`.
   - Restores this package while Android package manager remains usable; may avert recurrence (subject to the 300s polling interval and boot ephemerality limits).
2. **Tailscale & Termux Watchdog (every 20s)**:
   - Reconnects Tailscale VPN if absent.
   - Cycles Termux app if port 8000 `/healthz` fails repeatedly.
   - Strictly respects the `/sdcard/.phone-server-paused` manual stop marker.

---

## 2. Publication & Deployment Commands (Coordinating Agent Authority)

Per repository contributor rules, "root reviews / root owns live deployment" denotes the coordinating agent holding deployment authority, not requiring Linux root permissions on the host or phone. The deployment helper runs with ordinary ADB authorization (Android shell UID 2000 on device, standard user on laptop).

### A. Publish & Activate via ADB Helper
From the repository root on the laptop:
```bash
python3 tools/ensure_phone_guardian.py --apply
```
This command:
- Verifies that `scripts/phone-server-guardian.sh` matches the committed `HEAD` and manifest sha256.
- Confirms target serial `d7e31d7a`, product `raphaelin`, and shell UID `2000`.
- Validates live hash precondition (only reviewed old hash `a4fe5f29...` or candidate allowed).
- Pushes candidate to `.stage` file and verifies staged sha256 BEFORE stopping any existing guardian.
- Stops running guardian only after verified candidate via proven PID identity (`SIGTERM`).
- Atomically renames staged candidate to live path with mode `0700` (ensuring cleanup on any failure).
- Launches detached under `nohup` and verifies new running PID.

### B. Manual Verification on Device
To verify the FindDevice safeguard one-shot:
```bash
adb -s d7e31d7a shell /data/local/tmp/phone-server-guardian.sh --check-finddevice
```

### C. Enable Laptop Periodic Rearm Timer
To ensure the guardian is restarted after phone reboots when connected to the laptop:
```bash
cp laptop/phone-guardian.service laptop/phone-guardian.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now phone-guardian.timer
```

---

## 3. Test Suite
Run the focused test suites:
```bash
python3 -m unittest tests/test_phone_server_guardian.py tests/test_ensure_phone_guardian.py
```

---

## 4. Known Operational Limitations
1. **Boot Ephemerality**: Non-root Android `shell` (UID 2000) processes do not persist across phone reboots without an ADB host trigger. The guardian will remain inactive after a reboot until the laptop rearm timer runs or root manually launches it.
2. **Disable-and-Reboot Window**: Periodic 300s polling cannot prevent a bootloop if an actor uninstalls or disables `com.xiaomi.finddevice` and immediately reboots the phone before the next guardian tick.
3. **USB Connection Dependency**: The automatic rearm mechanism depends on an authorized USB connection between the phone and the laptop.
4. **Target Isolation**: All ADB automation strictly pins serial `d7e31d7a` and refuses to touch secondary devices (e.g. OPPO `SK7L7TDQKNBM9HW4`).
