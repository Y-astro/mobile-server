import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARDIAN_SCRIPT = REPO_ROOT / "scripts" / "phone-server-guardian.sh"


class TestPhoneServerGuardian(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.tmp_dir.name)
        self.bin_dir = self.test_dir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.test_dir / "state"
        self.log_file = self.state_dir / "events.log"
        self.calls_file = self.test_dir / "cmd_calls.log"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_fake_cmd(
        self,
        list_output="package:com.xiaomi.finddevice\n",
        exit_code=0,
        install_output="Package com.xiaomi.finddevice installed for user: 0\n",
        install_code=0,
        enable_output="Package com.xiaomi.finddevice new state: enabled\n",
        enable_code=0,
    ):
        cmd_script = self.bin_dir / "cmd"
        content = f"""#!/bin/sh
echo "$@" >> "{self.calls_file}"
if [ "$1" = "package" ] && [ "$2" = "list" ]; then
    printf "{list_output}"
    exit {exit_code}
elif [ "$1" = "package" ] && [ "$2" = "install-existing" ]; then
    printf "{install_output}"
    exit {install_code}
elif [ "$1" = "package" ] && [ "$2" = "enable" ]; then
    printf "{enable_output}"
    exit {enable_code}
fi
exit 0
"""
        cmd_script.write_text(content)
        cmd_script.chmod(cmd_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _create_fake_service(self, output="Service package: found\n", exit_code=0):
        service_script = self.bin_dir / "service"
        content = f"""#!/bin/sh
echo "$@" >> "{self.calls_file}"
if [ "$1" = "check" ] && [ "$2" = "package" ]; then
    printf "{output}"
    exit {exit_code}
fi
exit 0
"""
        service_script.write_text(content)
        service_script.chmod(service_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _run_check(self, env_extra=None):
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env["STATE"] = str(self.state_dir)
        if env_extra:
            env.update(env_extra)

        return subprocess.run(
            ["/bin/sh", str(GUARDIAN_SCRIPT), "--check-finddevice"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_installed_enabled_guard_noop(self):
        self._create_fake_service()
        self._create_fake_cmd(list_output="package:com.xiaomi.finddevice\n", exit_code=0)

        res = self._run_check()
        self.assertEqual(res.returncode, 0, f"Expected 0, got {res.returncode}. Stderr: {res.stderr}")

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertTrue(any("package list packages -e --user 0 com.xiaomi.finddevice" in c for c in calls))
        self.assertFalse(any("install-existing" in c for c in calls))
        self.assertFalse(any("package enable" in c for c in calls))
        self.assertFalse(self.log_file.exists(), "Healthy check must not write log spam")

    def test_missing_guard_restores_only_finddevice_user_0(self):
        self._create_fake_service()
        self._create_fake_cmd(list_output="", exit_code=0)

        res = self._run_check()
        self.assertEqual(res.returncode, 0, f"Expected 0, got {res.returncode}. Stderr: {res.stderr}")

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        install_calls = [c for c in calls if "install-existing" in c]
        enable_calls = [c for c in calls if "enable" in c]

        self.assertEqual(len(install_calls), 1)
        self.assertEqual(install_calls[0].strip(), "package install-existing --user 0 com.xiaomi.finddevice")

        self.assertEqual(len(enable_calls), 1)
        self.assertEqual(enable_calls[0].strip(), "package enable --user 0 com.xiaomi.finddevice")

        self.assertTrue(self.log_file.exists())
        log_content = self.log_file.read_text()
        self.assertIn("OS-critical FindDevice absent or disabled for user 0; restoring", log_content)

    def test_failed_package_query_does_not_restore(self):
        self._create_fake_service()
        self._create_fake_cmd(list_output="cmd: Can't find service: package\n", exit_code=20)

        res = self._run_check()
        self.assertNotEqual(res.returncode, 0)

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("install-existing" in c for c in calls))
        self.assertFalse(any("enable" in c for c in calls))

        self.assertTrue(self.log_file.exists())
        log_content = self.log_file.read_text()
        self.assertIn("Package framework query failed", log_content)

    def test_service_package_unavailable_does_not_restore(self):
        self._create_fake_service(output="Service package: not found\n", exit_code=0)
        self._create_fake_cmd(list_output="", exit_code=0)

        res = self._run_check()
        self.assertNotEqual(res.returncode, 0)

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("install-existing" in c for c in calls))
        self.assertFalse(any("enable" in c for c in calls))

        self.assertTrue(self.log_file.exists())
        log_content = self.log_file.read_text()
        self.assertIn("Package framework unavailable", log_content)

    def test_no_other_package_writes(self):
        self._create_fake_service()
        self._create_fake_cmd(list_output="", exit_code=0)

        res = self._run_check()
        self.assertEqual(res.returncode, 0)

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        for call in calls:
            if "install-existing" in call or "enable" in call:
                self.assertIn("com.xiaomi.finddevice", call)
                self.assertNotIn("other", call)

    def test_failed_install_existing_propagates_and_skips_enable(self):
        self._create_fake_service()
        self._create_fake_cmd(list_output="", exit_code=0, install_output="Failure [INSTALL_FAILED]\n", install_code=1)

        res = self._run_check()
        self.assertNotEqual(res.returncode, 0, "Failed install must propagate non-zero exit")

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertTrue(any("install-existing" in c for c in calls))
        self.assertFalse(any("enable" in c for c in calls), "Must NOT attempt enable after failed install")

        self.assertTrue(self.log_file.exists())
        log_content = self.log_file.read_text()
        self.assertIn("Failed to install-existing com.xiaomi.finddevice", log_content)

    def test_failed_enable_propagates(self):
        self._create_fake_service()
        self._create_fake_cmd(list_output="", exit_code=0, install_code=0, enable_output="Error: could not enable\n", enable_code=1)

        res = self._run_check()
        self.assertNotEqual(res.returncode, 0, "Failed enable must propagate non-zero exit")

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertTrue(any("install-existing" in c for c in calls))
        self.assertTrue(any("enable" in c for c in calls))

        self.assertTrue(self.log_file.exists())
        log_content = self.log_file.read_text()
        self.assertIn("Failed to enable com.xiaomi.finddevice", log_content)

    def test_unexpected_nonempty_error_output_fails_closed(self):
        self._create_fake_service()
        self._create_fake_cmd(list_output="Error: unexpected framework warning\n", exit_code=0)

        res = self._run_check()
        self.assertNotEqual(res.returncode, 0, "Unexpected non-empty output must fail-closed")

        calls = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        self.assertFalse(any("install-existing" in c for c in calls), "Must NOT restore on unexpected output")
        self.assertFalse(any("enable" in c for c in calls))

        self.assertTrue(self.log_file.exists())
        log_content = self.log_file.read_text()
        self.assertIn("Package framework query returned unexpected output", log_content)

    def test_log_rotation_helper(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Create an oversized log file (> 131072 bytes)
        self.log_file.write_text("x" * 131073)

        self._create_fake_service()
        self._create_fake_cmd(list_output="", exit_code=0)

        res = self._run_check()
        self.assertEqual(res.returncode, 0)

        rotated = self.state_dir / "events.log.1"
        self.assertTrue(rotated.exists())
        self.assertEqual(len(rotated.read_text()), 131073)
        self.assertTrue(self.log_file.exists())
        self.assertIn("OS-critical FindDevice absent or disabled for user 0; restoring", self.log_file.read_text())

    def test_clock_cadence_variables(self):
        content = GUARDIAN_SCRIPT.read_text()
        self.assertIn("last_finddevice_check=0", content)
        self.assertIn("300", content)


if __name__ == "__main__":
    unittest.main()
