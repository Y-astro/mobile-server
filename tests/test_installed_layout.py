import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class InstalledLayoutImportTests(unittest.TestCase):
    def test_dashboard_imports_in_installed_layout(self):
        """Verify dashboard runs when installed to bin/dashboard and helpers reside

        only in ~/.local/lib/mobile-server, with no repository on PYTHONPATH.
        """
        repo_root = Path(__file__).resolve().parents[1]
        dashboard_src = repo_root / "app" / "dashboard.py"
        app_dir = repo_root / "app"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fake_home = tmp / "home"
            fake_bin = tmp / "bin"
            fake_lib = fake_home / ".local" / "lib" / "mobile-server"
            fake_lib.mkdir(parents=True)
            fake_bin.mkdir(parents=True)

            # Copy dashboard executable to bin/dashboard (non-module location)
            installed_dashboard = fake_bin / "dashboard"
            shutil.copy(dashboard_src, installed_dashboard)

            # Copy only helper modules to fake_lib
            # Copy all flat app/*.py except dashboard to fake_lib
            required_helpers = [
                "notes.py",
                "document_conversion.py",
                "auth.py",
                "telemetry.py",
                "limits.py",
                "tor_proxy.py",
                "laptop.py",
                "agy_sessions.py",
                "service_panel.py",
            ]
            for py_file in app_dir.glob("*.py"):
                if py_file.name == "dashboard.py":
                    continue
                shutil.copy(py_file, fake_lib / py_file.name)

            for req in required_helpers:
                self.assertTrue(
                    (fake_lib / req).is_file(),
                    f"Required helper {req} is missing from installed fake_lib",
                )

            smoke_script = f"""
import importlib.util, os, sys, runpy
from pathlib import Path

fake_home = Path({repr(str(fake_home))})
fake_lib = fake_home / ".local" / "lib" / "mobile-server"
os.environ["HOME"] = str(fake_home)
dash_path = {repr(str(installed_dashboard))}

# Audit hook to reject network, subprocess, or process kill attempts in child
def audit_hook(event, args):
    if event in ("socket.connect", "subprocess.Popen", "os.kill", "os.killpg"):
        raise PermissionError(f"Prohibited child action in test harness: {{event}}")
sys.addaudithook(audit_hook)

# Disable Telemetry.start BEFORE runpy without modifying sys.path early
# Load actual installed telemetry.py via spec_from_file_location
telemetry_path = fake_lib / "telemetry.py"
assert telemetry_path.is_file(), f"telemetry.py missing at {{telemetry_path}}"
spec = importlib.util.spec_from_file_location("telemetry", str(telemetry_path))
telemetry_mod = importlib.util.module_from_spec(spec)
sys.modules["telemetry"] = telemetry_mod
spec.loader.exec_module(telemetry_mod)

# Replace/mock ONLY Telemetry.start to avoid background thread / network probes
telemetry_mod.Telemetry.start = lambda self: None

# Execute dashboard with non-__main__ run_name so ThreadedServer serve_forever is avoided
mod = runpy.run_path(dash_path, run_name="isolated_dashboard")

# Verify notes and document_conversion helper symbols are resolved
NotesStore = mod["NotesStore"]
convert_documents_batch = mod["convert_documents_batch"]
assert NotesStore.__module__ == "notes"
assert convert_documents_batch.__module__ == "document_conversion"

# Verify imported module file locations point to installed library directory
# Verify exact installed module directory for both extracted helpers
import notes, document_conversion
assert Path(notes.__file__).resolve().parent == fake_lib.resolve(), (
    f"notes.__file__ parent {{Path(notes.__file__).resolve().parent}} != {{fake_lib.resolve()}}"
)
assert Path(document_conversion.__file__).resolve().parent == fake_lib.resolve(), (
    f"document_conversion.__file__ parent {{Path(document_conversion.__file__).resolve().parent}} != {{fake_lib.resolve()}}"
)

# Verify notes bindings operate on temp HOME path
# Verify NOTES_STORE exact temporary notes path and synthetic notes roundtrip
expected_notes_path = fake_home / ".config" / "dashboard" / "notes.json"
notes_store = mod["NOTES_STORE"]
assert Path(notes_store.path).resolve() == expected_notes_path.resolve(), (
    f"NOTES_STORE path {{notes_store.path}} != {{expected_notes_path}}"
)
assert mod["set_user_notes"]("installed_user", "sample payload") is True
assert mod["get_user_notes"]("installed_user") == "sample payload"

print("installed imports and notes roundtrip passed; telemetry disabled")
"""
            # Launch isolated python without inheriting repository PYTHONPATH (-I)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(fake_home)}
            proc = subprocess.run(
                [sys.executable, "-I", "-c", smoke_script],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
                cwd=fake_home,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Dashboard failed to load in installed layout:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
