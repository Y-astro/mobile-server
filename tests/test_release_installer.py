#!/usr/bin/env python3
"""
tests/test_release_installer.py: Focused verification for scripts/install.sh wrapper.

Validates:
1. Fail-fast guard on no-argument invocation before any tool execution.
2. Rejection of unknown actions, duplicate flags, missing flags, and invalid combinations.
3. Fail-closed refusal when sibling release_activation.py adapter is absent.
4. Exact argv forwarding with preservation of spaces and shell metacharacters.
5. Exit code preservation from downstream activation adapter.
6. Isolation from external system binaries (caddy, cp, install, mktemp, service).
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"


def make_canary_bin(bin_dir: Path, name: str, sentinel_path: Path):
    path = bin_dir / name
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"#!/bin/sh\necho '{name}' >> '{sentinel_path}'\nexit 1\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_no_argument_guard():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        bin_dir = tdp / "bin"
        bin_dir.mkdir()
        sentinel = tdp / "sentinel.txt"
        for tool in ("caddy", "filebrowser", "tor", "tmux", "cp", "install", "mktemp", "python3", "service"):
            make_canary_bin(bin_dir, tool, sentinel)

        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
        proc = subprocess.run(
            ["bash", str(INSTALL_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        assert proc.returncode == 1, f"Expected 1, got {proc.returncode}"
        assert "Direct source writes via legacy installer are disabled" in proc.stderr, (
            f"Expected guard message in stderr, got: {proc.stderr}"
        )
        assert not sentinel.exists(), "Canary tool was executed during no-arg guard!"


def test_invalid_actions():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        bin_dir = tdp / "bin"
        bin_dir.mkdir()
        sentinel = tdp / "sentinel.txt"
        for tool in ("caddy", "cp", "install", "mktemp", "python3"):
            make_canary_bin(bin_dir, tool, sentinel)

        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")

        for bad_action in ("rollback", "recover", "status", "--bundle", "unknown"):
            proc = subprocess.run(
                ["bash", str(INSTALL_SCRIPT), bad_action],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            assert proc.returncode != 0, f"Action '{bad_action}' unexpectedly succeeded"
            assert not sentinel.exists(), f"External tool invoked on invalid action '{bad_action}'"


def test_malformed_arguments():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        bin_dir = tdp / "bin"
        bin_dir.mkdir()
        sentinel = tdp / "sentinel.txt"
        for tool in ("caddy", "cp", "install", "mktemp", "python3"):
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
            # Missing value for flag
            ["plan", "--bundle"],
            ["apply", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--transaction-dir"],
            # Unknown flag
            ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "--extra-flag"],
            # Trailing unknown positional argument
            ["plan", "--bundle", "/tmp/b", "--preconditions", "/tmp/p", "extra_arg"],
        ]

        for argv in cases:
            proc = subprocess.run(
                ["bash", str(INSTALL_SCRIPT)] + argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            assert proc.returncode != 0, f"Command {argv} unexpectedly succeeded"
            assert not sentinel.exists(), f"Canary executed on malformed invocation: {argv}"


def test_missing_adapter_refusal():
    # In this isolated branch, scripts/release_activation.py is absent.
    # The wrapper must fail closed before calling python3.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        bin_dir = tdp / "bin"
        bin_dir.mkdir()
        sentinel = tdp / "sentinel.txt"
        make_canary_bin(bin_dir, "python3", sentinel)

        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
        proc = subprocess.run(
            ["bash", str(INSTALL_SCRIPT), "plan", "--bundle", "/tmp/bundle", "--preconditions", "/tmp/pre.json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        assert proc.returncode == 1, f"Expected 1, got {proc.returncode}"
        assert "reviewed release activation adapter not found" in proc.stderr
        assert not sentinel.exists(), "Canary python3 was executed when adapter was absent!"


def test_exact_argv_and_metacharacter_forwarding():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        bin_dir = tdp / "bin"
        bin_dir.mkdir()
        sentinel = tdp / "sentinel.txt"
        # Canaries for forbidden tools
        for tool in ("caddy", "cp", "install", "mktemp", "service"):
            make_canary_bin(bin_dir, tool, sentinel)

        # Stage scripts/install.sh and a fake sibling release_activation.py
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

        # Plan with spaces, quotes, and metacharacters
        bundle_arg = "/var/lib/my releases/bundle;touch bad*file"
        precond_arg = "/var/preconditions/file with spaces & symbols?.json"
        ns_arg = "/data/data/com.termux/files with spaces"

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
        assert proc.returncode == 0, f"Plan failed: {proc.stderr}"
        assert not sentinel.exists(), "Forbidden tools executed during plan"

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
        assert received == expected, f"Argv mismatch:\nExpected: {expected}\nReceived: {received}"

        # Apply with transaction-dir
        tx_arg = "/tmp/tx dir;rm -rf *"
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
        assert proc_apply.returncode == 0, f"Apply failed: {proc_apply.stderr}"
        assert not sentinel.exists(), "Forbidden tools executed during apply"

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
        assert received_apply == expected_apply, (
            f"Apply argv mismatch:\nExpected: {expected_apply}\nReceived: {received_apply}"
        )


def test_child_exit_code_propagation():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        staged_scripts = tdp / "scripts"
        staged_scripts.mkdir()
        shutil.copy2(INSTALL_SCRIPT, staged_scripts / "install.sh")

        fake_adapter = staged_scripts / "release_activation.py"
        with open(fake_adapter, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(42)\n")

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
        assert proc.returncode == 42, f"Expected 42, got {proc.returncode}"


def main():
    test_suite = [
        ("test_no_argument_guard", test_no_argument_guard),
        ("test_invalid_actions", test_invalid_actions),
        ("test_malformed_arguments", test_malformed_arguments),
        ("test_missing_adapter_refusal", test_missing_adapter_refusal),
        ("test_exact_argv_and_metacharacter_forwarding", test_exact_argv_and_metacharacter_forwarding),
        ("test_child_exit_code_propagation", test_child_exit_code_propagation),
    ]

    passed = 0
    failed = 0
    print("Running release installer tests...")
    for name, test_fn in test_suite:
        try:
            test_fn()
            print(f"  [PASS] {name}")
            passed += 1
        except Exception as exc:
            print(f"  [FAIL] {name}: {exc}")
            failed += 1

    print(f"\nResult: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
