import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENSURE_SCRIPT = REPO_ROOT / "tools" / "ensure_phone_guardian.py"
MANIFEST_FILE = REPO_ROOT / "config" / "phone-guardian.json"


class TestEnsurePhoneGuardian(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.tmp_dir.name)
        self.bin_dir = self.test_dir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.fake_adb = self.bin_dir / "adb"
        self.calls_file = self.test_dir / "adb_calls.log"
        self.adb_state = self.test_dir / "adb_state.json"

        # Setup fake git wrapper in bin_dir to return current working copy for git show HEAD
        self.fake_git = self.bin_dir / "git"
        git_content = f"""#!/bin/sh
if [ "$1" = "show" ]; then
    cat "{REPO_ROOT}/scripts/phone-server-guardian.sh"
    exit 0
fi
exec /usr/bin/git "$@"
"""
        self.fake_git.write_text(git_content)
        self.fake_git.chmod(self.fake_git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        # Initialize mock state
        self.manifest = json.loads(MANIFEST_FILE.read_text())
        self.target_serial = self.manifest["device"]["serial"]
        self.candidate_sha = self.manifest["hashes"]["candidate_sha256"]
        self.old_sha = self.manifest["hashes"]["allowed_old_sha256"]

        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\nSK7L7TDQKNBM9HW4\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": self.candidate_sha,
            "staged_sha": "",
            "pid_content": "1234",
            "pid_alive": True,
            "cmdline": "sh /data/local/tmp/phone-server-guardian.sh",
        })

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _set_mock_state(self, state_dict):
        self.adb_state.write_text(json.dumps(state_dict))
        self._write_fake_adb()

    def _get_mock_state(self):
        return json.loads(self.adb_state.read_text())

    def _write_fake_adb(self):
        content = f"""#!/usr/bin/env python3
import sys, json, os, hashlib

calls_file = "{self.calls_file}"
state_file = "{self.adb_state}"
with open(calls_file, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")

with open(state_file, "r") as f:
    state = json.load(f)

args = sys.argv[1:]
if not args:
    sys.exit(0)

if args[0] == "devices":
    sys.stdout.write("List of devices attached\\n" + state.get("devices", ""))
    sys.exit(0)

# Check target serial
if args[0] == "-s":
    serial = args[1]
    subargs = args[2:]
else:
    serial = None
    subargs = args

if subargs[0] == "shell":
    cmd_str = " ".join(subargs[1:])
    if "getprop ro.product" in cmd_str:
        sys.stdout.write(state.get("product", "raphaelin") + "\\n")
        sys.exit(0)
    elif "id -u" in cmd_str:
        sys.stdout.write(state.get("uid", "2000") + "\\n")
        sys.exit(0)
    elif "sha256sum" in cmd_str and "phone-server-guardian.sh.stage" in cmd_str:
        sha = state.get("staged_sha", "")
        if sha:
            sys.stdout.write(f"{{sha}}  /data/local/tmp/phone-server-guardian.sh.stage\\n")
        else:
            sys.stderr.write("No such file\\n")
            sys.exit(1)
        sys.exit(0)
    elif "sha256sum" in cmd_str:
        sha = state.get("live_sha", "")
        if sha:
            sys.stdout.write(f"{{sha}}  /data/local/tmp/phone-server-guardian.sh\\n")
        else:
            sys.stderr.write("No such file\\n")
            sys.exit(1)
        sys.exit(0)
    elif "cat /data/local/tmp/phone-server-guardian/pid" in cmd_str:
        pid = state.get("pid_content", "")
        if pid:
            sys.stdout.write(pid + "\\n")
        sys.exit(0)
    elif "kill -0" in cmd_str:
        if state.get("pid_alive", False):
            sys.stdout.write("alive\\n")
        sys.exit(0)
    elif "cmdline" in cmd_str:
        sys.stdout.write(state.get("cmdline", "") + "\\n")
        sys.exit(0)
    elif "kill -TERM" in cmd_str:
        state["pid_alive"] = False
        with open(state_file, "w") as f:
            json.dump(state, f)
        sys.exit(0)
    elif "nohup" in cmd_str:
        state["pid_content"] = "5678"
        state["pid_alive"] = True
        state["cmdline"] = "sh /data/local/tmp/phone-server-guardian.sh"
        with open(state_file, "w") as f:
            json.dump(state, f)
        sys.exit(0)
    elif "mv /data/local/tmp/phone-server-guardian.sh.stage" in cmd_str:
        state["live_sha"] = state.get("staged_sha", "")
        state["staged_sha"] = ""
        with open(state_file, "w") as f:
            json.dump(state, f)
        sys.exit(0)
    elif "rm -f /data/local/tmp/phone-server-guardian.sh.stage" in cmd_str:
        state["staged_sha"] = ""
        with open(state_file, "w") as f:
            json.dump(state, f)
        sys.exit(0)
    elif "rm -f" in cmd_str:
        sys.exit(0)
    elif "chmod" in cmd_str:
        sys.exit(0)

if subargs[0] == "push":
    if state.get("push_fail", False):
        sys.stderr.write("Simulated push failure\\n")
        sys.exit(1)
    src_file = subargs[1]
    if os.path.exists(src_file):
        with open(src_file, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
        state["staged_sha"] = state.get("override_staged_sha", file_hash)
    with open(state_file, "w") as f:
        json.dump(state, f)
    sys.exit(0)

sys.exit(0)
"""
        self.fake_adb.write_text(content)
        self.fake_adb.chmod(self.fake_adb.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _run_cli(self, extra_args=None):
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        cmd = [str(ENSURE_SCRIPT)] + (extra_args or [])
        return subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_disconnected_noop(self):
        self._set_mock_state({
            "devices": "SK7L7TDQKNBM9HW4\tdevice\n",
        })
        res = self._run_cli(["--ensure"])
        self.assertEqual(res.returncode, 0)
        if self.calls_file.exists():
            calls = self.calls_file.read_text()
            self.assertNotIn("SK7L7TDQKNBM9HW4", calls)

    def test_healthy_ensure_idempotent(self):
        res = self._run_cli(["--ensure"])
        self.assertEqual(res.returncode, 0)
        calls = self.calls_file.read_text().splitlines()
        for call in calls:
            if call.startswith("devices"):
                continue
            self.assertTrue(call.startswith(f"-s {self.target_serial}"), f"Call did not target serial: {call}")
        self.assertFalse(any("nohup" in call for call in calls))

    def test_missing_ensure_starts_guardian(self):
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": self.candidate_sha,
            "pid_content": "",
            "pid_alive": False,
            "cmdline": "",
        })
        res = self._run_cli(["--ensure"])
        self.assertEqual(res.returncode, 0)
        calls = self.calls_file.read_text().splitlines()
        self.assertTrue(any("nohup" in call for call in calls))

    def test_ensure_never_overwrites_old_or_unknown_live_version(self):
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": self.old_sha,
            "pid_content": "9242",
            "pid_alive": False,
            "cmdline": "",
        })
        res = self._run_cli(["--ensure"])
        self.assertNotEqual(res.returncode, 0)
        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("push" in call for call in calls))

    def test_wrong_precondition_hash_fail_stops_no_push_start(self):
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": "unknown_unreviewed_hash_12345",
            "pid_content": "",
            "pid_alive": False,
        })
        res = self._run_cli(["--apply"])
        self.assertNotEqual(res.returncode, 0)
        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("push" in call for call in calls))
        self.assertFalse(any("nohup" in call for call in calls))

    def test_unknown_pid_not_killed(self):
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": self.old_sha,
            "pid_content": "8888",
            "pid_alive": True,
            "cmdline": "system_server",
        })
        res = self._run_cli(["--apply"])
        self.assertNotEqual(res.returncode, 0)
        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("kill -TERM" in call for call in calls))

    def test_apply_complete_success_from_old_hash(self):
        # Starts with old reviewed hash and active running PID 1234
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": self.old_sha,
            "staged_sha": "",
            "pid_content": "1234",
            "pid_alive": True,
            "cmdline": "sh /data/local/tmp/phone-server-guardian.sh",
        })

        res = self._run_cli(["--apply"])
        self.assertEqual(res.returncode, 0, f"Expected 0, got {res.returncode}. Stderr: {res.stderr}")

        calls = self.calls_file.read_text().splitlines()
        # Verify push happened BEFORE kill -TERM
        push_indices = [i for i, c in enumerate(calls) if "push" in c]
        term_indices = [i for i, c in enumerate(calls) if "kill -TERM" in c]
        mv_indices = [i for i, c in enumerate(calls) if "mv /data/local/tmp/phone-server-guardian.sh.stage" in c]
        launch_indices = [i for i, c in enumerate(calls) if "nohup" in c]

        self.assertTrue(len(push_indices) >= 1)
        self.assertTrue(len(term_indices) >= 1)
        self.assertTrue(len(mv_indices) >= 1)
        self.assertTrue(len(launch_indices) >= 1)

        # Stage push must happen before kill -TERM
        self.assertLess(push_indices[0], term_indices[0], "Push must happen BEFORE stopping old guardian")
        # Kill -TERM must happen before mv
        self.assertLess(term_indices[0], mv_indices[0], "Stopping old guardian must happen before atomic mv")

        final_state = self._get_mock_state()
        self.assertEqual(final_state["live_sha"], self.candidate_sha)
        self.assertEqual(final_state["pid_content"], "5678")
        self.assertTrue(final_state["pid_alive"])

    def test_staged_push_fail_does_not_kill_running_guardian(self):
        # Running old guardian
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": self.old_sha,
            "pid_content": "1234",
            "pid_alive": True,
            "cmdline": "sh /data/local/tmp/phone-server-guardian.sh",
            "push_fail": True,
        })

        res = self._run_cli(["--apply"])
        self.assertNotEqual(res.returncode, 0)

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("kill -TERM" in c for c in calls), "Push failure must NOT kill running guardian")

        final_state = self._get_mock_state()
        self.assertTrue(final_state["pid_alive"], "Running guardian must remain alive on push failure")

    def test_staged_bad_sha_does_not_kill_running_guardian(self):
        # Running old guardian, but staged push results in corrupted hash
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "product": "raphaelin",
            "uid": "2000",
            "live_sha": self.old_sha,
            "pid_content": "1234",
            "pid_alive": True,
            "cmdline": "sh /data/local/tmp/phone-server-guardian.sh",
            "override_staged_sha": "corrupted_staged_hash_9999",
        })

        res = self._run_cli(["--apply"])
        self.assertNotEqual(res.returncode, 0)

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("kill -TERM" in c for c in calls), "Staged hash mismatch must NOT kill running guardian")

        final_state = self._get_mock_state()
        self.assertTrue(final_state["pid_alive"], "Running guardian must remain alive on staged hash mismatch")
        self.assertEqual(final_state.get("staged_sha"), "", "Staged file must be cleaned up on failure")

    def test_reject_spoofed_cmdline_and_pid_0_1(self):
        from tools.ensure_phone_guardian import is_verified_guardian_cmdline, get_running_guardian_pid

        script = "/data/local/tmp/phone-server-guardian.sh"

        # Valid cmdlines
        self.assertTrue(is_verified_guardian_cmdline("/data/local/tmp/phone-server-guardian.sh", script))
        self.assertTrue(is_verified_guardian_cmdline("sh /data/local/tmp/phone-server-guardian.sh", script))
        self.assertTrue(is_verified_guardian_cmdline("/system/bin/sh /data/local/tmp/phone-server-guardian.sh", script))
        self.assertTrue(is_verified_guardian_cmdline("toybox sh /data/local/tmp/phone-server-guardian.sh", script))

        # Spoofed cmdline: sh -c 'echo phone-server-guardian.sh'
        self.assertFalse(is_verified_guardian_cmdline("sh -c echo phone-server-guardian.sh", script))
        self.assertFalse(is_verified_guardian_cmdline("/bin/sh -c 'phone-server-guardian.sh'", script))
        self.assertFalse(is_verified_guardian_cmdline("grep phone-server-guardian.sh", script))

        # PID 0 and 1 rejection
        self._set_mock_state({
            "devices": f"{self.target_serial}\tdevice\n",
            "pid_content": "1",
            "pid_alive": True,
            "cmdline": "sh /data/local/tmp/phone-server-guardian.sh",
        })
        self.assertIsNone(get_running_guardian_pid(str(self.fake_adb), self.target_serial, "/data/local/tmp/phone-server-guardian/pid", script))

    def test_apply_source_uncommitted_check_fails(self):
        from tools.ensure_phone_guardian import verify_source_committed
        with tempfile.TemporaryDirectory() as td:
            t_root = Path(td)
            script_rel = "scripts/phone-server-guardian.sh"
            target_path = t_root / script_rel
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text("modified uncommitted content")
            content_sha = hashlib.sha256(b"modified uncommitted content").hexdigest()

            # Initialize a git repo where HEAD has different content
            subprocess.run(["git", "init"], cwd=str(t_root), check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(t_root), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(t_root), check=True)
            target_path.write_text("initial committed content")
            subprocess.run(["git", "add", "."], cwd=str(t_root), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(t_root), check=True, stdout=subprocess.DEVNULL)

            # Now modify working copy
            target_path.write_text("modified uncommitted content")
            with self.assertRaises(RuntimeError) as cm:
                verify_source_committed(t_root, script_rel, content_sha)
            self.assertIn("uncommitted changes", str(cm.exception))


class TestGuardianShutdownBoundary(unittest.TestCase):
    def test_shutdown_allows_android_shell_to_finish_sleep(self):
        from unittest.mock import patch
        from tools import ensure_phone_guardian as guardian
        alive_checks = 0

        def shell(adb, serial, command):
            nonlocal alive_checks
            output = ""
            if command.startswith("cat "):
                output = "12345\n"
            elif command.startswith("kill -0 "):
                alive_checks += 1
                output = "alive\n" if alive_checks <= 13 else ""
            elif command.startswith("tr "):
                output = "/system/bin/sh /data/local/tmp/phone-server-guardian.sh"
            return subprocess.CompletedProcess([], 0, output, "")

        with patch.object(guardian, "run_adb_shell", side_effect=shell), patch.object(guardian.time, "sleep"):
            guardian.stop_proven_guardian("adb", "test-serial", "/owned/pid", "/data/local/tmp/phone-server-guardian.sh")
        self.assertGreater(alive_checks, 11)

    def test_same_script_name_in_another_directory_is_not_owned(self):
        from tools.ensure_phone_guardian import is_verified_guardian_cmdline
        self.assertFalse(is_verified_guardian_cmdline(
            "/system/bin/sh /another/phone-server-guardian.sh",
            "/data/local/tmp/phone-server-guardian.sh",
        ))


if __name__ == "__main__":
    unittest.main()
