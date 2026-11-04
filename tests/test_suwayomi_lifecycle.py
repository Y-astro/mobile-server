"""Focused unit and integration tests for Suwayomi runtime lifecycle ownership.

Verifies:
1. scripts/start_suwayomi.sh shell syntax, argv/env preservation, working directory,
   LD_PRELOAD DNS fix, and removal of background filter/scheduler forks.
2. scripts/server_supervisor.py matching for suwayomi_scheduler: selects same-UID
   actual owned Python script in exactly foreground/daemon mode with PID/starttime;
   rejects -c/-m decoys, unrelated paths, --now/--status, and zombies.
3. scripts/server_supervisor.py health checking for suwayomi_scheduler: requires one
   actual owned live process, matching recorded PID file, and held singleton flock
   using strictly non-creating/read-only checks; rejects duplicate instances, stale
   PID files, and unlocked files.
4. Generic Supervisor lifecycle recovery and disabled flag enforcement for
   suwayomi_scheduler without foreign daemonization wrappers.
"""

import builtins
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_PATH = ROOT / "scripts/server_supervisor.py"

# Guard parent_pid.txt write on import
_orig_open = builtins.open


def _guarded_open(file, *args, **kwargs):
    if str(file) == "/data/data/com.termux/files/home/parent_pid.txt":
        raise OSError("guarded test environment")
    return _orig_open(file, *args, **kwargs)


builtins.open = _guarded_open
try:
    spec = importlib.util.spec_from_file_location("server_supervisor", SUPERVISOR_PATH)
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
finally:
    builtins.open = _orig_open


class SuwayomiLauncherLifecycleTests(unittest.TestCase):
    """Characterization and stub-execution tests for start_suwayomi.sh."""

    def test_shell_syntax(self):
        """Shell syntax is valid according to bash -n."""
        script = ROOT / "scripts/start_suwayomi.sh"
        res = subprocess.run(
            ["/bin/bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(res.returncode, 0, f"Bash syntax check failed: {res.stderr}")

    def test_no_background_forks_in_script_source(self):
        """Launcher contains no backgrounding (&) calls for filter or scheduler."""
        script_text = (ROOT / "scripts/start_suwayomi.sh").read_text()
        self.assertNotIn("suwayomi_daily_update.py", script_text)
        self.assertNotIn("suwayomi_graphql_filter.py", script_text)
        self.assertNotIn("pgrep", script_text)

    def test_launcher_native_java_exec_and_env_with_stub(self):
        """Launcher passes exact JVM_ARGS, working directory, and LD_PRELOAD to native java."""
        script_content = (ROOT / "scripts/start_suwayomi.sh").read_text()
        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td)
            fake_home = tpath / "home"
            fake_prefix = tpath / "usr"
            (fake_home / "suwayomi/data").mkdir(parents=True)
            (fake_home / "suwayomi/bin").mkdir(parents=True)
            (fake_prefix / "bin").mkdir(parents=True)
            (fake_prefix / "tmp").mkdir(parents=True)

            stub_log = tpath / "java_exec.json"
            stub_java = fake_prefix / "bin/java"
            stub_java.write_text(
                f"""#!/bin/sh
python3 -c "import sys, os, json
data = {{
    'argv': sys.argv[1:],
    'cwd': os.getcwd(),
    'env': dict(os.environ),
}}
with open('{stub_log}', 'w') as f:
    json.dump(data, f)
" "$@"
"""
            )
            stub_java.chmod(0o755)

            # Point PREFIX and HOME to isolated temp directories
            test_script = script_content.replace(
                'export PREFIX="/data/data/com.termux/files/usr"',
                f'export PREFIX="{fake_prefix}"',
            ).replace(
                'export HOME="/data/data/com.termux/files/home"',
                f'export HOME="{fake_home}"',
            )
            test_sh = tpath / "test_start.sh"
            test_sh.write_text(test_script)
            test_sh.chmod(0o755)

            res = subprocess.run(
                ["/bin/bash", str(test_sh)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(res.returncode, 0, f"Launcher failed: {res.stderr}")

            self.assertTrue(stub_log.is_file(), "Native java stub was not executed")
            data = json.loads(stub_log.read_text())

            # Working directory
            self.assertEqual(data["cwd"], str(fake_home / "suwayomi"))

            # LD_PRELOAD DNS fix
            self.assertEqual(
                data["env"].get("LD_PRELOAD"),
                f"{fake_home}/suwayomi/libdns_fix.so",
            )

            # JVM Arguments
            expected_jvm_args = [
                "-XX:+UseSerialGC",
                "-XX:TieredStopAtLevel=1",
                "-XX:CICompilerCount=2",
                "-Xms64m",
                "-Xmx384m",
                "-Dh2.defaultCacheSize=65536",
                "-Dh2.maxMemoryRows=50000",
                "-Dfile.encoding=UTF-8",
                "-Dsun.jnu.encoding=UTF-8",
                "-Djava.awt.headless=true",
                f"-Dsuwayomi.tachidesk.config.server.rootDir={fake_home}/suwayomi/data",
            ]
            for arg in expected_jvm_args:
                self.assertIn(arg, data["argv"])

            # Jar argument
            jar_idx = data["argv"].index("-jar")
            self.assertEqual(
                data["argv"][jar_idx + 1],
                str(fake_home / "suwayomi/bin/Suwayomi-Server.jar"),
            )

    def test_launcher_proot_fallback_exec_and_env_with_stub(self):
        """When native java is missing, launcher falls back to proot-distro debian."""
        script_content = (ROOT / "scripts/start_suwayomi.sh").read_text()
        with tempfile.TemporaryDirectory() as td:
            tpath = Path(td)
            fake_home = tpath / "home"
            fake_prefix = tpath / "usr"
            (fake_home / "suwayomi/data").mkdir(parents=True)
            (fake_home / "suwayomi/bin").mkdir(parents=True)
            (fake_prefix / "bin").mkdir(parents=True)
            (fake_prefix / "tmp").mkdir(parents=True)

            stub_log = tpath / "proot_exec.json"
            stub_proot = fake_prefix / "bin/proot-distro"
            stub_proot.write_text(
                f"""#!/bin/sh
python3 -c "import sys, os, json
data = {{
    'argv': sys.argv[1:],
    'cwd': os.getcwd(),
    'env': dict(os.environ),
}}
with open('{stub_log}', 'w') as f:
    json.dump(data, f)
" "$@"
"""
            )
            stub_proot.chmod(0o755)

            test_script = script_content.replace(
                'export PREFIX="/data/data/com.termux/files/usr"',
                f'export PREFIX="{fake_prefix}"',
            ).replace(
                'export HOME="/data/data/com.termux/files/home"',
                f'export HOME="{fake_home}"',
            )
            test_sh = tpath / "test_start.sh"
            test_sh.write_text(test_script)
            test_sh.chmod(0o755)

            res = subprocess.run(
                ["/bin/bash", str(test_sh)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(res.returncode, 0, f"Launcher fallback failed: {res.stderr}")

            self.assertTrue(stub_log.is_file(), "proot-distro stub was not executed")
            data = json.loads(stub_log.read_text())

            # proot command structure
            self.assertEqual(data["argv"][:2], ["login", "debian"])
            self.assertIn("--work-dir", data["argv"])
            work_dir_idx = data["argv"].index("--work-dir")
            self.assertEqual(data["argv"][work_dir_idx + 1], str(fake_home / "suwayomi"))

            # proot java command
            self.assertIn("/usr/bin/java", data["argv"])
            self.assertIn("-XX:+UseSerialGC", data["argv"])
            self.assertIn(f"-Dsuwayomi.tachidesk.config.server.rootDir={fake_home}/suwayomi/data", data["argv"])


class SuwayomiSchedulerMatchingTests(unittest.TestCase):
    """Process matching tests with isolated mock procfs tree."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc_dir = self.root / "proc"
        self.proc_dir.mkdir()
        self.home_dir = self.root / "home"
        self.home_dir.mkdir()
        (self.home_dir / "bin").mkdir()

        self.orig_proc = supervisor.PROC
        self.orig_home = supervisor.HOME
        supervisor.PROC = self.proc_dir
        supervisor.HOME = self.home_dir

    def tearDown(self):
        supervisor.PROC = self.orig_proc
        supervisor.HOME = self.orig_home

    def _create_proc(self, pid: int, argv, cwd_path=None, state="S", starttime="1000", uid=None):
        p = self.proc_dir / str(pid)
        p.mkdir(parents=True, exist_ok=True)
        (p / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        (p / "stat").write_text(f"{pid} (python3) {state}" + " 0" * 18 + f" {starttime} 0 0\n")
        if cwd_path is not None:
            os.symlink(str(cwd_path), str(p / "cwd"))
        return p

    def test_matches_configured_absolute_foreground(self):
        """Matches python3 with configured absolute path and --foreground."""
        self._create_proc(
            101,
            [
                "/data/data/com.termux/files/usr/bin/python3",
                str(self.home_dir / "bin/suwayomi_daily_update.py"),
                "--foreground",
            ],
            starttime="1001",
        )
        matches = supervisor.matching_processes("suwayomi_scheduler")
        self.assertEqual(matches, [(101, "1001")])

    def test_matches_configured_absolute_daemon(self):
        """Matches python3 with configured absolute path and --daemon."""
        self._create_proc(
            102,
            [
                "/data/data/com.termux/files/usr/bin/python3",
                str(self.home_dir / "bin/suwayomi_daily_update.py"),
                "--daemon",
            ],
            starttime="1002",
        )
        matches = supervisor.matching_processes("suwayomi_scheduler")
        self.assertEqual(matches, [(102, "1002")])

    def test_matches_safely_resolved_relative_script_via_process_cwd(self):
        """Matches relative script name when process cwd is the owned home/bin."""
        self._create_proc(
            103,
            ["python3", "suwayomi_daily_update.py", "--daemon"],
            cwd_path=self.home_dir / "bin",
            starttime="1003",
        )
        matches = supervisor.matching_processes("suwayomi_scheduler")
        self.assertEqual(matches, [(103, "1003")])

    def test_matches_relative_script_from_home_directory(self):
        """Matches relative bin/script path when process cwd is home."""
        self._create_proc(
            104,
            ["python3", "bin/suwayomi_daily_update.py", "--foreground"],
            cwd_path=self.home_dir,
            starttime="1004",
        )
        matches = supervisor.matching_processes("suwayomi_scheduler")
        self.assertEqual(matches, [(104, "1004")])

    def test_rejects_c_decoy(self):
        """Rejects python -c invocation containing script string."""
        self._create_proc(
            201,
            [
                "python3",
                "-c",
                "import suwayomi_daily_update",
                str(self.home_dir / "bin/suwayomi_daily_update.py"),
                "--foreground",
            ],
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_m_decoy(self):
        """Rejects python -m invocation."""
        self._create_proc(
            202,
            ["python3", "-m", "suwayomi_daily_update", "--foreground"],
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_unrelated_absolute_path(self):
        """Rejects script path outside owned home/bin."""
        self._create_proc(
            203,
            ["python3", "/tmp/suwayomi_daily_update.py", "--foreground"],
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_relative_script_in_unrelated_cwd(self):
        """Rejects relative script when process cwd is an unrelated directory."""
        self._create_proc(
            204,
            ["python3", "suwayomi_daily_update.py", "--daemon"],
            cwd_path=self.root,
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_now_argument(self):
        """Rejects explicit manual update invocation --now."""
        self._create_proc(
            205,
            [
                "python3",
                str(self.home_dir / "bin/suwayomi_daily_update.py"),
                "--now",
            ],
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_status_argument(self):
        """Rejects status check invocation --status."""
        self._create_proc(
            206,
            [
                "python3",
                str(self.home_dir / "bin/suwayomi_daily_update.py"),
                "--status",
            ],
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_missing_mode_argument(self):
        """Rejects invocation without either --foreground or --daemon."""
        self._create_proc(
            207,
            ["python3", str(self.home_dir / "bin/suwayomi_daily_update.py")],
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_both_foreground_and_daemon_mode(self):
        """Rejects ambiguous invocation passing both --foreground and --daemon."""
        self._create_proc(
            208,
            [
                "python3",
                str(self.home_dir / "bin/suwayomi_daily_update.py"),
                "--foreground",
                "--daemon",
            ],
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])

    def test_rejects_zombie_process(self):
        """Rejects zombie processes (stat state Z) even if cmdline matches."""
        self._create_proc(
            209,
            [
                "python3",
                str(self.home_dir / "bin/suwayomi_daily_update.py"),
                "--foreground",
            ],
            state="Z",
        )
        self.assertEqual(supervisor.matching_processes("suwayomi_scheduler"), [])


class SuwayomiSchedulerHealthTests(unittest.TestCase):
    """Health check tests verifying flock, PID file matching, and read-only checks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home_dir = self.root / "home"
        self.pid_file = self.home_dir / "suwayomi/data/scheduler.pid"
        self.pid_file.parent.mkdir(parents=True)

        self.orig_home = supervisor.HOME
        self.orig_pid_file = supervisor.SCHEDULER_PID_FILE
        supervisor.HOME = self.home_dir
        supervisor.SCHEDULER_PID_FILE = self.pid_file
        self.cfg = {"cmd": ["python3", "suwayomi_daily_update.py", "--foreground"]}

    def tearDown(self):
        supervisor.HOME = self.orig_home
        supervisor.SCHEDULER_PID_FILE = self.orig_pid_file

    def test_healthy_when_one_process_pid_matches_and_flock_held(self):
        """Healthy when exactly one live process matches, recorded PID matches, and flock is held."""
        self.pid_file.write_text("5555\n")
        lock_fd = open(self.pid_file, "a+")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(supervisor, "matching_processes", return_value=[(5555, "ident")]):
                self.assertTrue(supervisor.healthy("suwayomi_scheduler", self.cfg))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def test_unhealthy_when_no_process_matching(self):
        """Unhealthy when no matching process is found."""
        self.pid_file.write_text("5555\n")
        with patch.object(supervisor, "matching_processes", return_value=[]):
            self.assertFalse(supervisor.healthy("suwayomi_scheduler", self.cfg))

    def test_unhealthy_when_duplicate_processes_matching(self):
        """Unhealthy when multiple matching processes exist (rejects ambiguous duplicate state)."""
        self.pid_file.write_text("5555\n")
        lock_fd = open(self.pid_file, "a+")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(supervisor, "matching_processes", return_value=[(5555, "id1"), (5556, "id2")]):
                self.assertFalse(supervisor.healthy("suwayomi_scheduler", self.cfg))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def test_unhealthy_when_pid_mismatch_stale_pid_file(self):
        """Unhealthy when PID in file does not match the single matching process."""
        self.pid_file.write_text("8888\n")
        lock_fd = open(self.pid_file, "a+")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(supervisor, "matching_processes", return_value=[(5555, "ident")]):
                self.assertFalse(supervisor.healthy("suwayomi_scheduler", self.cfg))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def test_unhealthy_when_flock_not_held(self):
        """Unhealthy when PID matches but singleton lock is not held (process died or unheld)."""
        self.pid_file.write_text("5555\n")
        # Ensure file is unlocked
        with patch.object(supervisor, "matching_processes", return_value=[(5555, "ident")]):
            self.assertFalse(supervisor.healthy("suwayomi_scheduler", self.cfg))

    def test_unhealthy_when_pid_file_missing(self):
        """Unhealthy when PID file does not exist on disk."""
        if self.pid_file.exists():
            self.pid_file.unlink()
        with patch.object(supervisor, "matching_processes", return_value=[(5555, "ident")]):
            self.assertFalse(supervisor.healthy("suwayomi_scheduler", self.cfg))

    def test_health_check_is_strictly_non_creating(self):
        """Health check performs no file or directory creation on disk."""
        non_existent_target = self.root / "completely/absent/path/scheduler.pid"
        supervisor.SCHEDULER_PID_FILE = non_existent_target
        self.assertFalse(non_existent_target.exists())
        self.assertFalse(non_existent_target.parent.exists())

        with patch.object(supervisor, "matching_processes", return_value=[(5555, "ident")]):
            result = supervisor.healthy("suwayomi_scheduler", self.cfg)
            self.assertFalse(result)

        # File and parent directories must not have been created
        self.assertFalse(non_existent_target.exists())
        self.assertFalse(non_existent_target.parent.exists())

    def test_unhealthy_when_pid_file_corrupt_or_empty(self):
        """Unhealthy when PID file contains non-numeric text or is empty."""
        for bad_content in ("", "   ", "not_a_pid\n", "-5555\n", "5555 6666\n"):
            self.pid_file.write_text(bad_content)
            with patch.object(supervisor, "matching_processes", return_value=[(5555, "ident")]):
                self.assertFalse(supervisor.healthy("suwayomi_scheduler", self.cfg), f"Expected False for {bad_content!r}")

    def test_battery_guard_health_semantics_preserved(self):
        """Battery guard continues using status.json mtime and matching_processes."""
        bg_status = self.home_dir / ".config/battery_guard/status.json"
        bg_status.parent.mkdir(parents=True, exist_ok=True)
        bg_status.write_text('{"battery_level": 80}')

        with patch.object(supervisor, "matching_processes", return_value=[(100, "ident")]):
            self.assertTrue(supervisor.healthy("battery_guard", {}))

        # Stale battery_guard status (> 360s) is unhealthy
        old_time = 100.0
        os.utime(bg_status, (old_time, old_time))
        with patch.object(supervisor, "matching_processes", return_value=[(100, "ident")]):
            self.assertFalse(supervisor.healthy("battery_guard", {}))


class SuwayomiSupervisorLifecycleTests(unittest.TestCase):
    """Generic supervisor recovery and disable tests for suwayomi_scheduler."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.disabled_dir = self.root / "disabled"
        self.state_dir.mkdir(parents=True)
        self.disabled_dir.mkdir(parents=True)

        self.orig_state = supervisor.STATE
        self.orig_disabled = supervisor.DISABLED
        self.orig_paused = supervisor.PAUSED
        supervisor.STATE = self.state_dir
        supervisor.DISABLED = self.disabled_dir
        supervisor.PAUSED = self.state_dir / "paused"

        self.cfg = {"cmd": ["/fake/python3", "/fake/suwayomi_daily_update.py", "--foreground"]}
        self.services = {"suwayomi_scheduler": self.cfg}

    def tearDown(self):
        supervisor.STATE = self.orig_state
        supervisor.DISABLED = self.orig_disabled
        supervisor.PAUSED = self.orig_paused

    def test_ensure_skips_disabled_scheduler(self):
        """Initial ensure records disabled state and never launches disabled scheduler."""
        (supervisor.DISABLED / "suwayomi_scheduler").touch()
        with patch.object(supervisor, "services", return_value=self.services), \
             patch.object(supervisor, "healthy") as mock_healthy, \
             patch.object(supervisor, "launch") as mock_launch:
            s = supervisor.Supervisor()
            s.cycle(initial=True)
            mock_healthy.assert_not_called()
            mock_launch.assert_not_called()

    def test_initial_ensure_launches_dead_scheduler(self):
        """Initial ensure launches scheduler when process is dead and service is not disabled."""
        with patch.object(supervisor, "services", return_value=self.services), \
             patch.object(supervisor, "healthy", return_value=False), \
             patch.object(supervisor, "matching_processes", return_value=[]), \
             patch.object(supervisor, "launch") as mock_launch:
            s = supervisor.Supervisor()
            s.cycle(initial=True)
            mock_launch.assert_called_once_with("suwayomi_scheduler", self.cfg)

    def test_normal_cycle_recovers_after_three_failures(self):
        """Normal monitoring cycle triggers recovery after 3 consecutive health failures."""
        with patch.object(supervisor, "services", return_value=self.services), \
             patch.object(supervisor, "healthy", return_value=False), \
             patch.object(supervisor, "matching_processes", return_value=[]), \
             patch.object(supervisor, "launch") as mock_launch:
            s = supervisor.Supervisor()
            s.cycle(initial=False)
            mock_launch.assert_not_called()
            s.cycle(initial=False)
            mock_launch.assert_not_called()
            s.cycle(initial=False)
            mock_launch.assert_called_once_with("suwayomi_scheduler", self.cfg)

    def test_normal_cycle_does_not_restart_healthy_scheduler(self):
        """Normal monitoring cycle does not launch or restart when scheduler is healthy."""
        with patch.object(supervisor, "services", return_value=self.services), \
             patch.object(supervisor, "healthy", return_value=True), \
             patch.object(supervisor, "launch") as mock_launch:
            s = supervisor.Supervisor()
            s.cycle(initial=False)
            s.cycle(initial=False)
            mock_launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
