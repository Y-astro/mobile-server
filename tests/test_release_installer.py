#!/usr/bin/env python3
"""
tests/test_release_installer.py: Focused verification for scripts/install.sh wrapper.

Validates:
1. Fail-fast guard on no-argument invocation before any tool execution.
2. Rejection of unknown actions, duplicate flags, missing flags, and invalid combinations.
3. Rejection of option-like / leading-dash values for path arguments.
4. Fail-closed refusal when sibling release_activation.py adapter is absent.
5. Exact argv forwarding with preservation of spaces, dollars, backticks, and subshells.
6. Downstream exit code propagation from child activation adapter.
7. Isolation from external system binaries (caddy, cp, install, mktemp, dirname, service).
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"


def make_canary_bin(bin_dir: Path, name: str, sentinel_path: Path):
    path = bin_dir / name
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#!/bin/sh\necho '{name}' >> '{sentinel_path}'\nexit 1\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class ReleaseInstallerTests(unittest.TestCase):
    """Test suite covering CLI argument parsing, security boundaries, and argv forwarding."""

    def test_no_argument_guard(self):
        """No-argument invocation must fail fast before resolving paths or calling tools."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Stage wrapper WITH fake sibling adapter so absence cannot mask parser defects
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")
            fake_adapter = staged_scripts / "release_activation.py"
            fake_adapter.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

            bin_dir = tdp / "bin"
            bin_dir.mkdir()
            sentinel = tdp / "sentinel.txt"
            for tool in ("caddy", "filebrowser", "tor", "tmux", "cp", "install", "mktemp", "python3", "dirname", "service"):
                make_canary_bin(bin_dir, tool, sentinel)

            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
            proc = subprocess.run(
                ["bash", str(staged_scripts / "install.sh")],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            self.assertEqual(proc.returncode, 1)
            self.assertIn("Direct source writes via legacy installer are disabled", proc.stderr)
            self.assertFalse(sentinel.exists(), "Canary tool executed during no-arg guard")

    def test_invalid_actions(self):
        """Disallowed or unrecognized positional actions must fail immediately."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")
            fake_adapter = staged_scripts / "release_activation.py"
            fake_adapter.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

            bin_dir = tdp / "bin"
            bin_dir.mkdir()
            sentinel = tdp / "sentinel.txt"
            for tool in ("caddy", "cp", "install", "mktemp", "python3", "dirname", "service"):
                make_canary_bin(bin_dir, tool, sentinel)

            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")

            for bad_action in ("rollback", "recover", "status", "--bundle", "unknown", "execute"):
                proc = subprocess.run(
                    ["bash", str(staged_scripts / "install.sh"), bad_action],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                self.assertNotEqual(proc.returncode, 0, f"Action '{bad_action}' unexpectedly succeeded")
                self.assertFalse(sentinel.exists(), f"Canary invoked on invalid action '{bad_action}'")

    def test_missing_and_option_like_values(self):
        """Missing, empty, or option-like leading-dash flag values must be rejected."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")
            fake_adapter = staged_scripts / "release_activation.py"
            fake_adapter.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

            bin_dir = tdp / "bin"
            bin_dir.mkdir()
            sentinel = tdp / "sentinel.txt"
            for tool in ("caddy", "cp", "install", "mktemp", "python3", "dirname", "service"):
                make_canary_bin(bin_dir, tool, sentinel)

            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")

            cases = [
                # Root acceptance reproduction: option-like flag passed where value expected
                ["plan", "--bundle", "--preconditions", "--preconditions", "/tmp/p"],
                # Missing argument value at end of line for all four flags
                ["plan", "--bundle"],
                ["plan", "--bundle", "/tmp/b", "--preconditions"],
                ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--transaction-dir"],
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--namespace-root"],
                # Empty string values for all four flags
                ["plan", "--bundle", "", "--preconditions", "/tmp/p"],
                ["plan", "--bundle", "/tmp/b", "--preconditions", ""],
                ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--transaction-dir", ""],
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--namespace-root", ""],
                # Option-like (leading dash) values for all four flags
                ["plan", "--bundle", "-bundle_path", "--preconditions", "/tmp/p"],
                ["plan", "--bundle", "/tmp/b", "--preconditions", "--precond_file"],
                ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--transaction-dir", "-tx_dir"],
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--namespace-root", "--root_dir"],
            ]

            for argv in cases:
                proc = subprocess.run(
                    ["bash", str(staged_scripts / "install.sh")] + argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                self.assertNotEqual(proc.returncode, 0, f"Malformed invocation {argv} unexpectedly succeeded")
                self.assertFalse(sentinel.exists(), f"Canary executed on malformed invocation {argv}")

    def test_malformed_combinations_and_duplicates(self):
        """Disallowed combinations, missing mandatory flags, and duplicate flags must be rejected."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")
            fake_adapter = staged_scripts / "release_activation.py"
            fake_adapter.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

            bin_dir = tdp / "bin"
            bin_dir.mkdir()
            sentinel = tdp / "sentinel.txt"
            for tool in ("caddy", "cp", "install", "mktemp", "python3", "dirname", "service"):
                make_canary_bin(bin_dir, tool, sentinel)

            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")

            cases = [
                # Missing mandatory bundle
                ["plan", "--preconditions", "/tmp/p.json"],
                # Missing mandatory preconditions
                ["plan", "--bundle", "/tmp/b"],
                # Apply missing mandatory transaction-dir
                ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p.json"],
                # Plan with transaction-dir (disallowed combination)
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p.json", "--transaction-dir", "/tmp/t"],
                # Duplicate bundle flag
                ["plan", "--bundle", "/tmp/b1", "--bundle", "/tmp/b2", "--preconditions", "/tmp/p.json"],
                # Duplicate preconditions flag
                ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p1", "--preconditions", "/tmp/p2", "--transaction-dir", "/tmp/t"],
                # Duplicate transaction-dir flag
                ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--transaction-dir", "/tmp/t1", "--transaction-dir", "/tmp/t2"],
                # Duplicate namespace-root flag
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--namespace-root", "/tmp/ns1", "--namespace-root", "/tmp/ns2"],
                # Unknown flag
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--extra-flag"],
                # Trailing unknown positional argument
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "extra_arg"],
            ]

            for argv in cases:
                proc = subprocess.run(
                    ["bash", str(staged_scripts / "install.sh")] + argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                self.assertNotEqual(proc.returncode, 0, f"Command {argv} unexpectedly succeeded")
                self.assertFalse(sentinel.exists(), f"Canary executed on malformed invocation: {argv}")

    def test_missing_adapter_refusal(self):
        """Absence of sibling release_activation.py must fail closed before calling python3."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Staged in separate directory with NO sibling adapter, independent of repo state
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")

            bin_dir = tdp / "bin"
            bin_dir.mkdir()
            sentinel = tdp / "sentinel.txt"
            make_canary_bin(bin_dir, "python3", sentinel)

            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
            proc = subprocess.run(
                ["bash", str(staged_scripts / "install.sh"), "plan", "--bundle", "/tmp/bundle", "--preconditions", "/tmp/pre.json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            self.assertEqual(proc.returncode, 1)
            self.assertIn("reviewed release activation adapter not found", proc.stderr)
            self.assertFalse(sentinel.exists(), "Canary python3 executed when adapter was absent")

    def test_exact_argv_and_metacharacter_forwarding(self):
        """Literal spaces, $(...), backticks, variables, and metacharacters must be forwarded without execution."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            bin_dir = tdp / "bin"
            bin_dir.mkdir()
            sentinel = tdp / "sentinel.txt"
            for tool in ("caddy", "cp", "install", "mktemp", "service"):
                make_canary_bin(bin_dir, tool, sentinel)

            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")

            log_path = tdp / "received_args.json"
            fake_adapter = staged_scripts / "release_activation.py"
            with open(fake_adapter, "w", encoding="utf-8") as f:
                f.write(
                    "import json, sys\n"
                    f"with open({repr(str(log_path))}, 'w', encoding='utf-8') as f:\n"
                    "    json.dump(sys.argv[1:], f)\n"
                    "sys.exit(0)\n"
                )

            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")

            subshell_sentinel = tdp / "evil_subshell_created.txt"
            backtick_sentinel = tdp / "evil_backtick_created.txt"

            bundle_arg = f"/var/lib/my releases/bundle;touch bad*file;$(touch {subshell_sentinel})"
            precond_arg = f"/var/preconditions/file with $VAR and `touch {backtick_sentinel}` & symbols?.json"
            ns_arg = "/data/data/com.termux/files with spaces and [wildcards]"

            proc = subprocess.run(
                [
                    "bash",
                    str(staged_scripts / "install.sh"),
                    "plan",
                    "--bundle",
                    bundle_arg,
                    "--preconditions",
                    precond_arg,
                    "--namespace-root",
                    ns_arg,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, f"Plan failed: {proc.stderr}")
            self.assertFalse(sentinel.exists(), "Forbidden tools executed during plan")
            self.assertFalse(subshell_sentinel.exists(), "Literal $(...) was executed as a shell command!")
            self.assertFalse(backtick_sentinel.exists(), "Literal backtick string was executed as a shell command!")

            with open(log_path, "r", encoding="utf-8") as f:
                received = json.load(f)

            expected = [
                "plan",
                "--bundle",
                bundle_arg,
                "--preconditions",
                precond_arg,
                "--namespace-root",
                ns_arg,
            ]
            self.assertEqual(received, expected)

            # Apply test with transaction-dir
            tx_arg = f"/tmp/tx dir;rm -rf *;`touch {backtick_sentinel}`"
            proc_apply = subprocess.run(
                [
                    "bash",
                    str(staged_scripts / "install.sh"),
                    "apply",
                    "--bundle",
                    bundle_arg,
                    "--preconditions",
                    precond_arg,
                    "--transaction-dir",
                    tx_arg,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.assertEqual(proc_apply.returncode, 0, f"Apply failed: {proc_apply.stderr}")
            self.assertFalse(sentinel.exists(), "Forbidden tools executed during apply")
            self.assertFalse(backtick_sentinel.exists(), "Backtick command executed during apply!")

            with open(log_path, "r", encoding="utf-8") as f:
                received_apply = json.load(f)

            expected_apply = [
                "apply",
                "--bundle",
                bundle_arg,
                "--preconditions",
                precond_arg,
                "--transaction-dir",
                tx_arg,
            ]
            self.assertEqual(received_apply, expected_apply)

    def test_child_exit_code_propagation(self):
        """Child exit code from release_activation.py must be propagated faithfully."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")

            for code in (42, 73):
                fake_adapter = staged_scripts / "release_activation.py"
                with open(fake_adapter, "w", encoding="utf-8") as f:
                    f.write(f"import sys\nsys.exit({code})\n")

                proc = subprocess.run(
                    [
                        "bash",
                        str(staged_scripts / "install.sh"),
                        "plan",
                        "--bundle",
                        "/tmp/bundle",
                        "--preconditions",
                        "/tmp/pre.json",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(proc.returncode, code)

    def test_profile_flag_validation(self):
        """Profile flag checks: missing value, empty, option-like, duplicate, and invalid value."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")
            fake_adapter = staged_scripts / "release_activation.py"
            fake_adapter.write_text("import sys; sys.exit(0)\n", encoding="utf-8")

            cases = [
                # Missing value at end
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--profile"],
                # Empty string value
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--profile", ""],
                # Option-like values
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--profile", "-general"],
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--profile", "--bundle"],
                # Duplicate flag
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--profile", "general", "--profile", "suwayomi-filter"],
                # Invalid profile values
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--profile", "unknown"],
                ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--profile", "suwayomi"],
                ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--transaction-dir", "/tmp/t", "--profile", "all"],
            ]

            for argv in cases:
                proc = subprocess.run(
                    ["bash", str(staged_scripts / "install.sh")] + argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertNotEqual(proc.returncode, 0, f"Malformed profile invocation {argv} unexpectedly succeeded")

    def test_profile_flag_forwarding(self):
        """Valid profile flags are forwarded accurately to release_activation.py."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            staged_scripts = tdp / "scripts"
            staged_scripts.mkdir()
            shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")

            log_path = tdp / "received_argv.json"
            fake_adapter = staged_scripts / "release_activation.py"
            with open(fake_adapter, "w", encoding="utf-8") as f:
                f.write(
                    "import json, sys\n"
                    f"with open('{log_path}', 'w', encoding='utf-8') as f:\n"
                    "    json.dump(sys.argv[1:], f)\n"
                    "sys.exit(0)\n"
                )

            # Test explicit --profile suwayomi-filter on plan
            proc_plan = subprocess.run(
                [
                    "bash",
                    str(staged_scripts / "install.sh"),
                    "plan",
                    "--bundle",
                    "/tmp/bundle_dir",
                    "--preconditions",
                    "/tmp/pre.json",
                    "--profile",
                    "suwayomi-filter",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(proc_plan.returncode, 0, f"Plan failed: {proc_plan.stderr}")
            with open(log_path, "r", encoding="utf-8") as f:
                received = json.load(f)
            self.assertEqual(
                received,
                ["plan", "--bundle", "/tmp/bundle_dir", "--preconditions", "/tmp/pre.json", "--profile", "suwayomi-filter"],
            )

            # Test explicit --profile general on apply
            proc_apply = subprocess.run(
                [
                    "bash",
                    str(staged_scripts / "install.sh"),
                    "apply",
                    "--bundle",
                    "/tmp/bundle_dir",
                    "--preconditions",
                    "/tmp/pre.json",
                    "--transaction-dir",
                    "/tmp/tx_dir",
                    "--profile",
                    "general",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(proc_apply.returncode, 0, f"Apply failed: {proc_apply.stderr}")
            with open(log_path, "r", encoding="utf-8") as f:
                received = json.load(f)
            self.assertEqual(
                received,
                ["apply", "--bundle", "/tmp/bundle_dir", "--preconditions", "/tmp/pre.json", "--transaction-dir", "/tmp/tx_dir", "--profile", "general"],
            )

            # Test omitted profile (default)
            proc_omit = subprocess.run(
                [
                    "bash",
                    str(staged_scripts / "install.sh"),
                    "plan",
                    "--bundle",
                    "/tmp/bundle_dir",
                    "--preconditions",
                    "/tmp/pre.json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(proc_omit.returncode, 0, f"Plan omitted profile failed: {proc_omit.stderr}")
            with open(log_path, "r", encoding="utf-8") as f:
                received = json.load(f)
            self.assertEqual(
                received,
                ["plan", "--bundle", "/tmp/bundle_dir", "--preconditions", "/tmp/pre.json"],
            )


if __name__ == "__main__":
    unittest.main()
