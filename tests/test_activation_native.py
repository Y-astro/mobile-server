import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import List

from scripts.release_activation import (
    MANDATORY_RECOVERY_ASSETS,
    ActivationError,
    preserve_recovery_lifecycle_code,
    validate_recovery_assets,
    apply,
)
import scripts.release_transaction as rt
import scripts.termux_activation as ta


def compute_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class SubprocessBootstrapProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tx_dir = Path(self.temp_dir.name)
        self.rec_dir = self.tx_dir / "recovery_lifecycle"
        self.rec_dir.mkdir(parents=True)
        self.scripts_dir = Path(__file__).resolve().parent.parent / "scripts"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_valid_recovery(self):
        repo_root = self.scripts_dir.parent
        shutil.copy2(self.scripts_dir / "release_activation.py", self.rec_dir / "release_activation.py")
        shutil.copy2(self.scripts_dir / "termux_activation.py", self.rec_dir / "termux_activation.py")
        shutil.copy2(self.scripts_dir / "release_transaction.py", self.rec_dir / "release_transaction.py")
        shutil.copy2(self.scripts_dir / "server_supervisor.py", self.rec_dir / "server_supervisor.py")
        cat_src = repo_root / "app/service_catalog.py"
        if not cat_src.exists():
            cat_src = self.scripts_dir / "service_catalog.py"
        shutil.copy2(cat_src, self.rec_dir / "service_catalog.py")

        rec_assets = {}
        for fname in MANDATORY_RECOVERY_ASSETS:
            rec_assets[fname] = compute_sha(self.rec_dir / fname)

        journal = {
            "version": 1,
            "top_uid": os.getuid(),
            "tx_id": "tx-probe-123",
            "transaction_dir": str(self.tx_dir),
            "namespace_root": str(self.tx_dir / "ns"),
            "status": "APPLIED",
            "recovery_assets": rec_assets,
        }
        with open(self.tx_dir / "activation_journal.json", "w", encoding="utf-8") as f:
            json.dump(journal, f)

    def test_missing_journal_refuses_before_sibling_execution(self):
        sentinel_flag = self.tx_dir / "sentinel_ran.txt"
        sentinel_code = 'from pathlib import Path\nPath(r"' + str(sentinel_flag) + '").write_text("PWNED")\n'
        with open(self.rec_dir / "termux_activation.py", "w") as f:
            f.write(sentinel_code)
        shutil.copy2(self.scripts_dir / "release_activation.py", self.rec_dir / "release_activation.py")

        cmd = [sys.executable, str(self.rec_dir / "release_activation.py"), "--help"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Recovery bootstrap refused: activation journal missing", res.stderr)
        self.assertFalse(sentinel_flag.exists(), "Sentinel sibling must never execute on missing journal")

    def test_corrupt_journal_refuses_before_sibling_execution(self):
        sentinel_flag = self.tx_dir / "sentinel_ran.txt"
        sentinel_code = 'from pathlib import Path\nPath(r"' + str(sentinel_flag) + '").write_text("PWNED")\n'
        with open(self.rec_dir / "termux_activation.py", "w") as f:
            f.write(sentinel_code)
        shutil.copy2(self.scripts_dir / "release_activation.py", self.rec_dir / "release_activation.py")

        with open(self.tx_dir / "activation_journal.json", "w", encoding="utf-8") as f:
            f.write("{corrupt_json")

        cmd = [sys.executable, str(self.rec_dir / "release_activation.py"), "--help"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Recovery bootstrap refused: invalid journal JSON", res.stderr)
        self.assertFalse(sentinel_flag.exists(), "Sentinel sibling must never execute on corrupt journal")

    def test_omitted_recorded_hash_refuses_before_sibling_execution(self):
        self._setup_valid_recovery()
        sentinel_flag = self.tx_dir / "sentinel_ran.txt"
        sentinel_code = 'from pathlib import Path\nPath(r"' + str(sentinel_flag) + '").write_text("PWNED")\n'
        with open(self.rec_dir / "termux_activation.py", "w") as f:
            f.write(sentinel_code)

        with open(self.tx_dir / "activation_journal.json", "r", encoding="utf-8") as f:
            jdata = json.load(f)
        del jdata["recovery_assets"]["termux_activation.py"]
        with open(self.tx_dir / "activation_journal.json", "w", encoding="utf-8") as f:
            json.dump(jdata, f)

        cmd = [sys.executable, str(self.rec_dir / "release_activation.py"), "--help"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("omitted recorded hash", res.stderr)
        self.assertFalse(sentinel_flag.exists(), "Sentinel must never execute when hash is omitted")

    def test_modified_file_hash_mismatch_refuses(self):
        self._setup_valid_recovery()
        sentinel_flag = self.tx_dir / "sentinel_ran.txt"
        sentinel_code = 'from pathlib import Path\nPath(r"' + str(sentinel_flag) + '").write_text("PWNED")\n'
        with open(self.rec_dir / "termux_activation.py", "w") as f:
            f.write(sentinel_code)

        cmd = [sys.executable, str(self.rec_dir / "release_activation.py"), "--help"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("hash mismatch", res.stderr)
        self.assertFalse(sentinel_flag.exists(), "Sentinel must never execute on hash mismatch")

    def test_symlink_asset_refuses(self):
        self._setup_valid_recovery()
        (self.rec_dir / "termux_activation.py").unlink()
        (self.rec_dir / "termux_activation.py").symlink_to(self.scripts_dir / "termux_activation.py")

        cmd = [sys.executable, str(self.rec_dir / "release_activation.py"), "--help"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("is not a regular file", res.stderr)

    def test_valid_copied_context_loads(self):
        self._setup_valid_recovery()
        cmd = [sys.executable, str(self.rec_dir / "release_activation.py"), "--help"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("usage:", res.stdout.lower())


class NamespaceIsolationAndDriftTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.ns1 = self.root / "ns1"
        self.ns2 = self.root / "ns2"
        for ns in (self.ns1, self.ns2):
            (ns / "home/bin").mkdir(parents=True)
            (ns / "usr").mkdir(parents=True)
            (ns / "home/.local/lib/mobile-server").mkdir(parents=True)
            (ns / "home/.config/server_recovery").mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_poisoned_service_catalog_restored_and_services_bound_to_temp_namespace(self):
        poison = type("PoisonCatalog", (), {"POISON": True})
        orig_catalog = sys.modules.get("service_catalog")
        sys.modules["service_catalog"] = poison

        try:
            driver1 = ta.TermuxLifecycleDriver(namespace_root=self.ns1)
            services1 = driver1.services()
            self.assertIn("dashboard", services1)
            cmd = services1["dashboard"]["cmd"]
            self.assertTrue(any(str(self.ns1) in token for token in cmd), f"Expected ns1 path in cmd: {cmd}")
            self.assertFalse(any("/data/data/com.termux/files/home" in token for token in cmd))

            self.assertTrue((self.ns1 / "home/parent_pid.txt").exists())

            driver2 = ta.TermuxLifecycleDriver(namespace_root=self.ns2)
            services2 = driver2.services()
            cmd2 = services2["dashboard"]["cmd"]
            self.assertTrue(any(str(self.ns2) in token for token in cmd2), f"Expected ns2 path in cmd: {cmd2}")
            self.assertIs(sys.modules.get("service_catalog"), poison)
        finally:
            if orig_catalog is not None:
                sys.modules["service_catalog"] = orig_catalog
            else:
                sys.modules.pop("service_catalog", None)

    def test_candidate_preflight_verifies_services_and_refuses_drift(self):
        driver = ta.TermuxLifecycleDriver(namespace_root=self.ns1)

        bundle_dir = self.root / "bundle"
        app_dir = bundle_dir / "app"
        app_dir.mkdir(parents=True)

        repo_cat = Path(__file__).resolve().parent.parent / "app/service_catalog.py"
        shutil.copy2(repo_cat, app_dir / "service_catalog.py")
        driver.validate_candidate_services(bundle_dir)

        original_stat = (app_dir / "service_catalog.py").stat()
        with open(app_dir / "service_catalog.py", "r") as f:
            code = f.read()
        altered_code = code.replace("'port': 8001", "'port': 9999")
        with open(app_dir / "service_catalog.py", "w") as f:
            f.write(altered_code)
        os.utime(app_dir / "service_catalog.py", ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        with self.assertRaises(RuntimeError) as ctx:
            driver.validate_candidate_services(bundle_dir)
        self.assertIn("runtime drift on 'port'", str(ctx.exception))

        with open(app_dir / "service_catalog.py", "w") as f:
            f.write("def broken_syntax(:")
        with self.assertRaises(SyntaxError):
            driver.validate_candidate_services(bundle_dir)

        (app_dir / "service_catalog.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            driver.validate_candidate_services(bundle_dir)


class DaemonProcessMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.proc_root = self.root / "proc"
        self.proc_root.mkdir()
        self.ns = self.root / "ns"
        (self.ns / "home/bin").mkdir(parents=True)
        (self.ns / "home/.config/server_recovery").mkdir(parents=True)
        self.sup_path = (self.ns / "home/bin/server_supervisor.py").resolve()
        shutil.copy2(Path(__file__).resolve().parent.parent / "scripts/server_supervisor.py", self.sup_path)
        self.driver = ta.TermuxLifecycleDriver(
            namespace_root=self.ns,
            supervisor_script=self.sup_path,
            proc_root=self.proc_root,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_fake_proc(self, pid: int, cmdline_tokens: List[str], cwd_path: Path, starttime: str = "12345"):
        pdir = self.proc_root / str(pid)
        pdir.mkdir(parents=True, exist_ok=True)
        raw_cmd = chr(0).join(cmdline_tokens).encode("utf-8") + b"\0"
        (pdir / "cmdline").write_bytes(raw_cmd)
        (pdir / "cwd").symlink_to(cwd_path)
        stat_content = f"{pid} (python3) S 1 1 1 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 {starttime} 1000 100"
        (pdir / "stat").write_text(stat_content)

    def test_relative_script_resolves_against_target_proc_cwd(self):
        self._create_fake_proc(
            pid=4242,
            cmdline_tokens=["python3", "bin/server_supervisor.py", "--daemon"],
            cwd_path=(self.ns / "home").resolve(),
        )
        match = self.driver.find_daemon_process()
        self.assertIsNotNone(match)
        self.assertEqual(match[0], 4242)

    def test_relative_script_decoy_cwd_rejected(self):
        self._create_fake_proc(
            pid=4243,
            cmdline_tokens=["python3", "bin/server_supervisor.py", "--daemon"],
            cwd_path=self.root.resolve(),
        )
        match = self.driver.find_daemon_process()
        self.assertIsNone(match)

    def test_decoy_option_rejected(self):
        self._create_fake_proc(
            pid=4244,
            cmdline_tokens=["python3", str(self.sup_path), "--config=--daemon"],
            cwd_path=(self.ns / "home").resolve(),
        )
        match = self.driver.find_daemon_process()
        self.assertIsNone(match)

    def test_multiple_matching_daemons_refuses_ambiguous(self):
        self._create_fake_proc(
            pid=4245,
            cmdline_tokens=["python3", str(self.sup_path), "--daemon"],
            cwd_path=(self.ns / "home").resolve(),
            starttime="1111",
        )
        self._create_fake_proc(
            pid=4246,
            cmdline_tokens=["python3", str(self.sup_path), "--daemon"],
            cwd_path=(self.ns / "home").resolve(),
            starttime="2222",
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.driver.find_daemon_process()
        self.assertIn("Multiple supervisor daemon processes detected", str(ctx.exception))

    def test_start_daemon_refuses_when_live_supervisor_missing(self):
        self.sup_path.unlink()
        with self.assertRaises(RuntimeError) as ctx:
            self.driver.start_daemon()
        self.assertIn("Restored live supervisor script not found", str(ctx.exception))

    def test_real_child_process_matching_and_termination(self):
        child_script = self.ns / "home/bin/server_supervisor.py"
        proc = subprocess.Popen(
            [sys.executable, "-u", str(child_script), "--daemon"],
            cwd=str(self.ns / "home"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            real_driver = ta.TermuxLifecycleDriver(
                namespace_root=self.ns,
                supervisor_script=child_script,
            )
            matched = real_driver.find_daemon_process()
            self.assertIsNotNone(matched)
            pid, ident = matched
            self.assertEqual(pid, proc.pid)

            real_driver.stop_daemon(pid, ident, timeout=2.0)
            proc.wait(timeout=3.0)
            self.assertFalse(proc.poll() is None)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class PreservedLifecycleBootstrapTests(unittest.TestCase):
    def test_preserved_loader_and_copied_cli_help_without_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            tx_dir = base / "tx"
            tx_dir.mkdir()
            ns_root = base / "ns"
            (ns_root / "home/bin").mkdir(parents=True)
            (ns_root / "home/.local/lib/mobile-server").mkdir(parents=True)
            (ns_root / "home/.config/server_recovery").mkdir(parents=True)
            (ns_root / "usr").mkdir(parents=True)

            scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
            repo_root = scripts_dir.parent

            shutil.copy2(scripts_dir / "server_supervisor.py", ns_root / "home/bin/server_supervisor.py")
            cat_src = repo_root / "app/service_catalog.py"
            shutil.copy2(cat_src, ns_root / "home/.local/lib/mobile-server/service_catalog.py")

            hashes = preserve_recovery_lifecycle_code(tx_dir, ns_root)
            self.assertEqual(set(hashes.keys()), set(MANDATORY_RECOVERY_ASSETS))

            validate_recovery_assets(tx_dir / "recovery_lifecycle", hashes, os.getuid())

            iso_driver = ta.load_isolated_lifecycle(
                recovery_dir=tx_dir / "recovery_lifecycle",
                namespace_root=ns_root,
                expected_hashes=hashes,
            )
            services = iso_driver.services()
            self.assertIn("dashboard", services)

            journal = {
                "version": 1,
                "top_uid": os.getuid(),
                "tx_id": "tx-rec-proof",
                "transaction_dir": str(tx_dir),
                "namespace_root": str(ns_root),
                "status": "APPLIED",
                "recovery_assets": hashes,
            }
            with open(tx_dir / "activation_journal.json", "w", encoding="utf-8") as f:
                json.dump(journal, f)

            rec_act = tx_dir / "recovery_lifecycle/release_activation.py"
            res = subprocess.run([sys.executable, str(rec_act), "--help"], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("usage:", res.stdout.lower())
