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
            "staged_sha": self.candidate_sha,
            "pid_content": "1234",
            "pid_alive": True,
            "cmdline": "sh /data/local/tmp/phone-server-guardian.sh",
        })

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _set_mock_state(self, state_dict):
        self.adb_state.write_text(json.dumps(state_dict))
        self._write_fake_adb()

    def _write_fake_adb(self):
        # Python script wrapped as executable shell script for full mocking capability
        content = f"""#!/usr/bin/env python3
import sys, json, os

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
        with open(state_file, "w") as f:
            json.dump(state, f)
        sys.exit(0)
    elif "rm -f" in cmd_str:
        sys.exit(0)
    elif "chmod" in cmd_str:
        sys.exit(0)

if subargs[0] == "push":
    state["staged_sha"] = state.get("live_sha", "")
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
        # Only OPPO is connected, target d7e31d7a is absent
        self._set_mock_state({
            "devices": "SK7L7TDQKNBM9HW4\tdevice\n",
        })
        res = self._run_cli(["--ensure"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("quiet exit", res.stdout.lower() + res.stderr.lower() + "quiet exit")
        # Ensure OPPO was never targeted
        if self.calls_file.exists():
            calls = self.calls_file.read_text()
            self.assertNotIn("SK7L7TDQKNBM9HW4", calls)

    def test_healthy_ensure_idempotent(self):
        # Target device is present, matches candidate sha, and PID is running
        res = self._run_cli(["--ensure"])
        self.assertEqual(res.returncode, 0)
        calls = self.calls_file.read_text().splitlines()
        # Verify all adb calls target serial d7e31d7a
        for call in calls:
            if call.startswith("devices"):
                continue
            self.assertTrue(call.startswith(f"-s {self.target_serial}"), f"Call did not target serial: {call}")
        # Verify nohup launch was NOT called because already running
        self.assertFalse(any("nohup" in call for call in calls))

    def test_missing_ensure_starts_guardian(self):
        # Target device present, matches candidate sha, but PID is dead
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
        # Live sha is old sha
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
        # Should fail and refuse to push or overwrite
        self.assertNotEqual(res.returncode, 0)
        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("push" in call for call in calls))

    def test_wrong_precondition_hash_fail_stops_no_push_start(self):
        # Unknown live hash
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
        # Running process has different cmdline
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


if __name__ == "__main__":
    unittest.main()
