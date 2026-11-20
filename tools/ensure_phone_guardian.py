#!/usr/bin/env python3
"""Host helper to publish and ensure phone-server-guardian on Redmi K20 Pro.

Usage:
  ensure_phone_guardian.py --ensure   # Read, validate, and rearm if missing (default)
  ensure_phone_guardian.py --apply    # Reviewed publication from committed git HEAD
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "config" / "phone-guardian.json"


def resolve_adb():
    """Resolve trusted adb executable."""
    which = shutil.which("adb")
    if which:
        return which
    for candidate in ["/usr/bin/adb", "/usr/local/bin/adb"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def load_manifest():
    if not MANIFEST_PATH.exists():
        raise RuntimeError(f"Manifest not found at {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text())


def check_device_status(adb_bin, target_serial, timeout=10):
    """Check device status from adb devices. Returns 'device', 'unauthorized', etc. or None."""
    try:
        res = subprocess.run(
            [adb_bin, "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == target_serial:
            return parts[1]
    return None


def run_adb_shell(adb_bin, target_serial, command_str, timeout=10):
    """Execute command in adb shell strictly targeting the configured serial with timeout."""
    cmd = [adb_bin, "-s", target_serial, "shell", command_str]
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        # Return synthetic failure on timeout
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr=f"Timed out after {timeout}s: {e}")


def validate_device_identity(adb_bin, target_serial, expected_product, expected_uid):
    """Validate device product model and shell UID before proceeding."""
    res = run_adb_shell(adb_bin, target_serial, "getprop ro.product.name")
    product = res.stdout.strip()
    if product != expected_product:
        res2 = run_adb_shell(adb_bin, target_serial, "getprop ro.product.device")
        if res2.stdout.strip() != expected_product:
            raise RuntimeError(f"Product mismatch: got {product!r}, expected {expected_product!r}")

    res_uid = run_adb_shell(adb_bin, target_serial, "id -u")
    uid_str = res_uid.stdout.strip()
    if uid_str != str(expected_uid):
        raise RuntimeError(f"Shell UID mismatch: got {uid_str!r}, expected {expected_uid!r}")


def verify_source_committed(repo_root, rel_script_path, expected_candidate_sha):
    """Verify that working tree script exactly matches committed HEAD and manifest sha256."""
    local_path = repo_root / rel_script_path
    if not local_path.exists():
        raise RuntimeError(f"Candidate script missing at {local_path}")

    local_bytes = local_path.read_bytes()
    local_sha = hashlib.sha256(local_bytes).hexdigest()
    if local_sha != expected_candidate_sha:
        raise RuntimeError(
            f"Candidate sha256 {local_sha} does not match manifest candidate_sha256 {expected_candidate_sha}"
        )

    git_res = subprocess.run(
        ["git", "show", f"HEAD:{rel_script_path}"],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    if git_res.returncode != 0:
        raise RuntimeError(
            f"git show HEAD:{rel_script_path} failed: {git_res.stderr.decode('utf-8', errors='replace')}"
        )

    head_sha = hashlib.sha256(git_res.stdout).hexdigest()
    if head_sha != local_sha:
        raise RuntimeError(
            f"Working copy has uncommitted changes! HEAD sha256 is {head_sha}, working copy is {local_sha}"
        )


def get_live_sha256(adb_bin, target_serial, remote_path):
    """Get remote file sha256 on target device, or None if absent."""
    res = run_adb_shell(
        adb_bin,
        target_serial,
        f"toybox sha256sum {remote_path} 2>/dev/null || sha256sum {remote_path} 2>/dev/null || true",
    )
    out = res.stdout.strip()
    if not out:
        return None
    return out.split()[0]


def is_verified_guardian_cmdline(cmdline_str, expected_script_path):
    """Verify that process cmdline executes guardian script as argv[0] or argv[1] of verified shell.

    Rejects spoof attempts such as: sh -c 'echo phone-server-guardian.sh'
    """
    if "\0" in cmdline_str:
        tokens = [t for t in cmdline_str.split("\0") if t]
    else:
        tokens = cmdline_str.strip().split()
    if not tokens:
        return False

    argv0 = tokens[0]

    # Direct execution as argv[0]
    if argv0 == expected_script_path:
        return True

    # Execution as argument to verified shell: sh <script> or toybox sh <script>
    shell_names = {"sh", "bash", "mksh", "toybox", "/system/bin/sh", "/bin/sh", "/system/bin/toybox"}
    if argv0 in shell_names or Path(argv0).name in {"sh", "bash", "mksh", "toybox"}:
        if len(tokens) >= 2 and tokens[1] == expected_script_path:
            return True
        if len(tokens) >= 3 and tokens[1] in {"sh", "bash", "mksh"}:
            if tokens[2] == expected_script_path:
                return True

    return False


def get_running_guardian_pid(adb_bin, target_serial, pid_file, expected_script_path):
    """Return verified running guardian PID, or None."""
    res = run_adb_shell(adb_bin, target_serial, f"cat {pid_file} 2>/dev/null || true")
    pid_str = res.stdout.strip()
    if not pid_str.isdigit():
        return None
    pid = int(pid_str)
    if pid <= 1:
        return None

    res_alive = run_adb_shell(adb_bin, target_serial, f"kill -0 {pid} 2>/dev/null && echo alive || true")
    if res_alive.stdout.strip() != "alive":
        return None

    res_cmd = run_adb_shell(adb_bin, target_serial, f"tr '\\000' ' ' < /proc/{pid}/cmdline 2>/dev/null || true")
    if not is_verified_guardian_cmdline(res_cmd.stdout, expected_script_path):
        return None

    return pid


def stop_proven_guardian(adb_bin, target_serial, pid_file, expected_script_path):
    """Stop guardian process only if proven to match phone-server-guardian.sh."""
    res = run_adb_shell(adb_bin, target_serial, f"cat {pid_file} 2>/dev/null || true")
    pid_str = res.stdout.strip()
    if not pid_str.isdigit():
        return

    pid = int(pid_str)
    if pid <= 1:
        raise RuntimeError(f"Invalid PID {pid} in {pid_file}; refusing to signal PID <= 1")

    res_alive = run_adb_shell(adb_bin, target_serial, f"kill -0 {pid} 2>/dev/null && echo alive || true")
    if res_alive.stdout.strip() != "alive":
        run_adb_shell(adb_bin, target_serial, f"rm -f {pid_file}")
        return

    res_cmd = run_adb_shell(adb_bin, target_serial, f"tr '\\000' ' ' < /proc/{pid}/cmdline 2>/dev/null || true")
    if not is_verified_guardian_cmdline(res_cmd.stdout, expected_script_path):
        raise RuntimeError(
            f"PID {pid} does not match verified guardian cmdline (got {res_cmd.stdout.strip()!r}); refusing to kill unknown PID"
        )

    run_adb_shell(adb_bin, target_serial, f"kill -TERM {pid}")
    # Android shell may defer its TERM trap until the current 20-second sleep ends.
    for _ in range(60):
        time.sleep(0.5)
        res_check = run_adb_shell(adb_bin, target_serial, f"kill -0 {pid} 2>/dev/null && echo alive || true")
        if res_check.stdout.strip() != "alive":
            break
    else:
        raise RuntimeError(f"Timed out waiting for guardian PID {pid} to terminate")

    run_adb_shell(adb_bin, target_serial, f"rm -f {pid_file}")


def launch_guardian(adb_bin, target_serial, live_path, console_log, pid_file):
    """Launch guardian detached in background under shell UID and verify PID."""
    cmd_launch = f"nohup {live_path} </dev/null >{console_log} 2>&1 &"
    run_adb_shell(adb_bin, target_serial, cmd_launch)
    time.sleep(1)
    pid = get_running_guardian_pid(adb_bin, target_serial, pid_file, live_path)
    if not pid:
        raise RuntimeError("Failed to verify phone guardian PID after launch")
    return pid


def handle_ensure(adb_bin, target_serial, manifest):
    """Ensure mode: read-validate-rearm. Never mutates files; exits 0 quietly if disconnected."""
    status = check_device_status(adb_bin, target_serial)
    if status != "device":
        # Disconnected, unauthorized, or absent: quiet exit 0
        return 0

    device_cfg = manifest["device"]
    paths = manifest["paths"]
    hashes = manifest["hashes"]

    validate_device_identity(adb_bin, target_serial, device_cfg["product"], device_cfg["required_uid"])

    live_sha = get_live_sha256(adb_bin, target_serial, paths["live_script"])
    if live_sha != hashes["candidate_sha256"]:
        sys.stderr.write(
            f"Live guardian sha256 ({live_sha}) does not match candidate ({hashes['candidate_sha256']}). "
            "--ensure will never overwrite or start unverified version. Use --apply to publish.\n"
        )
        return 1

    pid = get_running_guardian_pid(adb_bin, target_serial, paths["pid_file"], paths["live_script"])
    if pid:
        print(f"Phone server guardian is active and verified (PID {pid}).")
        return 0

    print("Phone server guardian absent or dead; starting verified candidate...")
    new_pid = launch_guardian(
        adb_bin,
        target_serial,
        paths["live_script"],
        paths["console_log"],
        paths["pid_file"],
    )
    print(f"Phone server guardian successfully started (PID {new_pid}).")
    return 0


def handle_apply(adb_bin, target_serial, manifest):
    """Apply mode: explicit reviewed publication. Stage+verify first, stop ONLY before atomic mv."""
    status = check_device_status(adb_bin, target_serial)
    if status != "device":
        raise RuntimeError(f"Target device {target_serial} not available (status: {status})")

    device_cfg = manifest["device"]
    paths = manifest["paths"]
    hashes = manifest["hashes"]

    # 1. Source committed check
    verify_source_committed(REPO_ROOT, paths["repo_script"], hashes["candidate_sha256"])

    # 2. Validate device product and UID
    validate_device_identity(adb_bin, target_serial, device_cfg["product"], device_cfg["required_uid"])

    # 3. Live hash precondition check
    live_sha = get_live_sha256(adb_bin, target_serial, paths["live_script"])
    allowed_hashes = {hashes["allowed_old_sha256"], hashes["candidate_sha256"]}
    if live_sha is not None and live_sha not in allowed_hashes:
        raise RuntimeError(
            f"Precondition failed: live sha256 {live_sha} is not in allowed reviewed hashes {allowed_hashes}"
        )

    local_script = str(REPO_ROOT / paths["repo_script"])
    staged_script = paths["staged_script"]
    live_script = paths["live_script"]

    # 4. Stage push and verify BEFORE stopping any existing guardian
    push_res = subprocess.run(
        [adb_bin, "-s", target_serial, "push", local_script, staged_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=15,
    )
    if push_res.returncode != 0:
        run_adb_shell(adb_bin, target_serial, f"rm -f {staged_script}")
        raise RuntimeError(f"Failed to push staged guardian: {push_res.stderr}")

    staged_sha = get_live_sha256(adb_bin, target_serial, staged_script)
    if staged_sha != hashes["candidate_sha256"]:
        run_adb_shell(adb_bin, target_serial, f"rm -f {staged_script}")
        raise RuntimeError(f"Staged file sha256 mismatch: got {staged_sha}, expected {hashes['candidate_sha256']}")

    # Candidate is verified in staging; now proceed to stop, swap, and launch
    try:
        # 5. Stop existing guardian if running
        stop_proven_guardian(adb_bin, target_serial, paths["pid_file"], live_script)

        # 6. Atomic rename and chmod
        rename_res = run_adb_shell(adb_bin, target_serial, f"mv {staged_script} {live_script} && chmod 0700 {live_script}")
        if rename_res.returncode != 0:
            raise RuntimeError(f"Failed to activate live guardian: {rename_res.stderr}")

        # 7. Launch and verify
        new_pid = launch_guardian(
            adb_bin,
            target_serial,
            live_script,
            paths["console_log"],
            paths["pid_file"],
        )
        print(f"Phone server guardian published and started (PID {new_pid}, SHA256: {hashes['candidate_sha256'][:16]}...).")
        return 0
    finally:
        # Cleanup staged file if any post-push step failed before or during move
        run_adb_shell(adb_bin, target_serial, f"rm -f {staged_script}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Publish verified committed guardian to phone and launch",
    )
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="Validate running guardian and rearm if absent (default)",
    )
    args = parser.parse_args()

    manifest = load_manifest()
    target_serial = manifest["device"]["serial"]

    adb_bin = resolve_adb()
    if not adb_bin:
        if args.apply:
            sys.stderr.write("adb executable not found.\n")
            return 1
        # Default/ensure: quiet exit 0 when adb is missing on host
        return 0

    if args.apply:
        return handle_apply(adb_bin, target_serial, manifest)
    else:
        return handle_ensure(adb_bin, target_serial, manifest)


if __name__ == "__main__":
    sys.exit(main())
