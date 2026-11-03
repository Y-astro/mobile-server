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
            helper_files = [
                "notes.py",
                "document_conversion.py",
                "auth.py",
                "telemetry.py",
                "limits.py",
                "tor_proxy.py",
                "laptop.py",
            ]
            for h in helper_files:
                src_file = app_dir / h
                if src_file.exists():
                    shutil.copy(src_file, fake_lib / h)

            smoke_script = f"""
import os, sys, runpy

fake_home = {repr(str(fake_home))}
os.environ["HOME"] = fake_home
dash_path = {repr(str(installed_dashboard))}

# Execute dashboard with non-__main__ run_name so ThreadedServer serve_forever is avoided
mod = runpy.run_path(dash_path, run_name="isolated_dashboard")

# Verify notes and document_conversion helper symbols are resolved
NotesStore = mod["NotesStore"]
convert_documents_batch = mod["convert_documents_batch"]
assert NotesStore.__module__ == "notes"
assert convert_documents_batch.__module__ == "document_conversion"

# Verify imported module file locations point to installed library directory
import notes, document_conversion
assert os.path.realpath(notes.__file__).startswith(os.path.realpath(str(fake_home)))
assert os.path.realpath(document_conversion.__file__).startswith(os.path.realpath(str(fake_home)))

# Verify notes bindings operate on temp HOME path
notes_store = mod["NOTES_STORE"]
assert str(fake_home) in notes_store.path
assert mod["set_user_notes"]("installed_user", "sample payload") is True
assert mod["get_user_notes"]("installed_user") == "sample payload"
"""
            # Launch isolated python without inheriting repository PYTHONPATH (-I)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(fake_home)}
            proc = subprocess.run(
                [sys.executable, "-I", "-c", smoke_script],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Dashboard failed to load in installed layout:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
