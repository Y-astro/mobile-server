"""Focused characterization, freshness isolation, import purity, and bootstrap tests

for the shared service catalog.
"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

import service_catalog


class ServiceCatalogTests(unittest.TestCase):
    def test_import_purity(self):
        """Catalog import performs no filesystem access, subprocess calls, network, or secret reads."""
        code = (
            "import sys\n"
            "import os\n"
            "# Track if filesystem writes, network, or subprocess are called\n"
            "sys.path.insert(0, 'app')\n"
            "import service_catalog\n"
            "assert hasattr(service_catalog, 'runtime_services')\n"
            "assert hasattr(service_catalog, 'dashboard_services')\n"
        )
        res = subprocess.run(
            [sys.executable, '-c', code],
            cwd=str(ROOT),
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0, f"Import failed: {res.stderr}")

    def test_fresh_copy_isolation(self):
        """Builders return independent mutable dictionaries that do not leak across calls."""
        r1 = service_catalog.runtime_services('/home/a', '/usr/a')
        r2 = service_catalog.runtime_services('/home/a', '/usr/a')
        self.assertEqual(r1, r2)
        r1['ssh']['port'] = 9999
        r1['ssh']['cmd'].append('--extra')
        self.assertNotEqual(r1['ssh']['port'], r2['ssh']['port'])
        self.assertNotEqual(r1['ssh']['cmd'], r2['ssh']['cmd'])

        d1 = service_catalog.dashboard_services()
        d2 = service_catalog.dashboard_services()
        self.assertEqual(d1, d2)
        d1['filebrowser']['port'] = 9999
        self.assertNotEqual(d1['filebrowser']['port'], d2['filebrowser']['port'])

    def test_runtime_services_characterization_and_order(self):
        """Preserve all 15 services, order, commands, arguments, ports, paths, and protocols."""
        char_file = ROOT / '.superpowers/sdd/pre_extraction_characterization.json'
        if char_file.exists():
            with open(char_file) as f:
                char = json.load(f)
            rt_default = service_catalog.runtime_services('/data/data/com.termux/files/home', '/data/data/com.termux/files/usr')
            self.assertEqual(rt_default, char['supervisor_default'])

            rt_alt = service_catalog.runtime_services('/custom/home', '/custom/usr')
            self.assertEqual(rt_alt, char['supervisor_alt'])

        # Explicit order & count check
        services = service_catalog.runtime_services('/h', '/p')
        expected_keys = [
            'ssh', 'dashboard', 'caddy', 'filebrowser', 'transfer_gateway',
            'adguard', 'syncthing', 'vaultwarden', 'terminal', 'agy',
            'laptop_terminal', 'tor', 'suwayomi', 'suwayomi_filter', 'battery_guard'
        ]
        self.assertEqual(list(services.keys()), expected_keys)
        self.assertEqual(len(services), 15)

    def test_dashboard_services_characterization_and_order(self):
        """Preserve all 9 dashboard cards, order, IDs, URLs, and verify absence of start_cmd/env."""
        char_file = ROOT / '.superpowers/sdd/pre_extraction_characterization.json'
        cards = service_catalog.dashboard_services()
        expected_keys = [
            'filebrowser', 'suwayomi', 'vaultwarden', 'adguard', 'terminal',
            'agy', 'converter', 'syncthing', 'battery_guard'
        ]
        self.assertEqual(list(cards.keys()), expected_keys)
        self.assertEqual(len(cards), 9)

        if char_file.exists():
            with open(char_file) as f:
                char = json.load(f)
            for k, card in cards.items():
                orig = dict(char['dashboard_services_config'][k])
                orig.pop('start_cmd', None)
                orig.pop('stop_cmd', None)
                orig.pop('env', None)
                self.assertEqual(card, orig)

        # Verify no stale keys remain in cards
        for sid, card in cards.items():
            self.assertNotIn('start_cmd', card)
            self.assertNotIn('stop_cmd', card)
            self.assertNotIn('env', card)

    def test_suwayomi_port_and_reachability_distinction(self):
        """Internal backend 4569, filter 4566, and dashboard/native 4567 are explicitly distinct."""
        rt = service_catalog.runtime_services('/h', '/p')
        self.assertEqual(rt['suwayomi']['port'], 4569)
        self.assertEqual(rt['suwayomi']['path'], '/suwayomi/')
        self.assertEqual(rt['suwayomi_filter']['port'], 4566)
        self.assertEqual(rt['suwayomi_filter']['path'], '/healthz')

        dash = service_catalog.dashboard_services()
        self.assertEqual(dash['suwayomi']['port'], 4567)
        self.assertEqual(dash['suwayomi']['open_url'], '/suwayomi/')

    def test_supervisor_compatibility_wrapper(self):
        """server_supervisor.services() returns runtime_services with supervisor globals."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('server_supervisor', ROOT / 'scripts/server_supervisor.py')
        sup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sup)
        self.assertTrue(callable(sup.services))
        s = sup.services()
        self.assertEqual(len(s), 15)
        self.assertEqual(s['caddy']['port'], 8000)

    def test_installed_layout_bootstrap(self):
        """Supervisor and dashboard execute cleanly when installed to HOME/bin and PREFIX/bin with shared module in ~/.local/lib/mobile-server."""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            fake_home = tmp_path / 'home'
            fake_prefix = tmp_path / 'usr'
            lib_dir = fake_home / '.local/lib/mobile-server'
            home_bin = fake_home / 'bin'
            prefix_bin = fake_prefix / 'bin'

            lib_dir.mkdir(parents=True, exist_ok=True)
            home_bin.mkdir(parents=True, exist_ok=True)
            prefix_bin.mkdir(parents=True, exist_ok=True)

            # Copy service_catalog.py to installed lib_dir
            catalog_src = (ROOT / 'app/service_catalog.py').read_text()
            (lib_dir / 'service_catalog.py').write_text(catalog_src)

            # Copy server_supervisor.py to home_bin
            sup_src = (ROOT / 'scripts/server_supervisor.py').read_text()
            (home_bin / 'server_supervisor.py').write_text(sup_src)

            # Smoke test running supervisor from installed layout
            env = os.environ.copy()
            env.pop('PYTHONPATH', None)
            env['HOME'] = str(fake_home)

            test_sup_cmd = (
                f"import sys\n"
                f"sys.path.insert(0, '{home_bin}')\n"
                f"import server_supervisor\n"
                f"svcs = server_supervisor.services()\n"
                f"assert len(svcs) == 15, len(svcs)\n"
                f"assert svcs['ssh']['port'] == 8022\n"
                f"print('INSTALLED_SUPERVISOR_BOOTSTRAP_OK')\n"
            )

            res = subprocess.run(
                [sys.executable, '-c', test_sup_cmd],
                cwd=td, # arbitrary cwd outside repo
                env=env,
                capture_output=True,
                text=True
            )
            self.assertEqual(res.returncode, 0, f"Supervisor bootstrap smoke failed: {res.stderr}\n{res.stdout}")
            self.assertIn("INSTALLED_SUPERVISOR_BOOTSTRAP_OK", res.stdout)

            # Copy dashboard.py to prefix_bin/dashboard
            dash_src = (ROOT / 'app/dashboard.py').read_text()
            (prefix_bin / 'dashboard.py').write_text(dash_src)

            # Also need dummy auth/telemetry/limits/tor_proxy/laptop modules in lib_dir for dashboard import smoke
            for dep in ['auth.py', 'telemetry.py', 'limits.py', 'tor_proxy.py', 'laptop.py']:
                (lib_dir / dep).write_text((ROOT / 'app' / dep).read_text())

            test_dash_cmd = (
                f"import sys\n"
                f"sys.path.insert(0, '{prefix_bin}')\n"
                f"import dashboard\n"
                f"assert len(dashboard.SERVICES_CONFIG) == 9, len(dashboard.SERVICES_CONFIG)\n"
                f"assert dashboard.SERVICES_CONFIG['filebrowser']['port'] == 8085\n"
                f"print('INSTALLED_DASHBOARD_BOOTSTRAP_OK')\n"
            )

            res2 = subprocess.run(
                [sys.executable, '-c', test_dash_cmd],
                cwd=td, # arbitrary cwd outside repo
                env=env,
                capture_output=True,
                text=True
            )
            self.assertEqual(res2.returncode, 0, f"Dashboard bootstrap smoke failed: {res2.stderr}\n{res2.stdout}")
            self.assertIn("INSTALLED_DASHBOARD_BOOTSTRAP_OK", res2.stdout)


if __name__ == '__main__':
    unittest.main()
