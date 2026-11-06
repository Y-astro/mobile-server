"""Focused characterization, freshness isolation, import purity, and bootstrap tests

for the shared service catalog.
"""

import copy
import json
import os
from pathlib import Path
import shutil
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
            "import sys, os, typing\n"
            "with open(sys.argv[1]) as f:\n"
            "    source = f.read()\n"
            "def audit_hook(event, args):\n"
            "    if event in ('open', 'os.mkdir', 'os.rename', 'os.remove', 'shutil.rmtree'):\n"
            "        raise PermissionError(f'Denied fs operation: {event}')\n"
            "    if event.startswith('subprocess.'):\n"
            "        raise PermissionError(f'Denied subprocess: {event}')\n"
            "    if event in ('socket.connect', 'socket.bind'):\n"
            "        raise PermissionError(f'Denied socket: {event}')\n"
            "    if event == '_thread.start_new_thread':\n"
            "        raise PermissionError(f'Denied thread start')\n"
            "sys.addaudithook(audit_hook)\n"
            "g = {}\n"
            "exec(compile(source, sys.argv[1], 'exec'), g)\n"
            "assert 'runtime_services' in g\n"
            "assert 'dashboard_services' in g\n"
            "print('EXEC_AUDIT_OK')\n"
        )
        catalog_src = str((ROOT / 'app/service_catalog.py').resolve())
        with tempfile.TemporaryDirectory() as td:
            res = subprocess.run(
                [sys.executable, '-I', '-c', code, catalog_src],
                cwd=td,
                capture_output=True,
                text=True,
                timeout=15
            )
            self.assertEqual(res.returncode, 0, f"Import purity audit failed: {res.stderr}\n{res.stdout}")
            self.assertIn("EXEC_AUDIT_OK", res.stdout)

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
        """Preserve all 15 services prefix, order, commands, arguments, ports, paths, protocols, and 16th scheduler."""
        char_file = ROOT / 'tests/fixtures/service-catalog-baseline.json'
        # Fixture is tracked; missing fixture must fail immediately
        self.assertTrue(char_file.is_file(), f"Missing required baseline fixture: {char_file}")
        with open(char_file) as f:
            char = json.load(f)

        baseline_keys = list(char['supervisor_default'].keys())
        self.assertEqual(len(baseline_keys), 15)

        rt_default = service_catalog.runtime_services('/data/data/com.termux/files/home', '/data/data/com.termux/files/usr')
        self.assertEqual(len(rt_default), 17)
        rt_default_prefix = {k: rt_default[k] for k in baseline_keys}
        self.assertEqual(rt_default_prefix, char['supervisor_default'])
        self.assertEqual(list(rt_default.keys())[:15], baseline_keys)
        self.assertEqual(list(rt_default.keys())[15], 'suwayomi_scheduler')
        self.assertEqual(rt_default['suwayomi_scheduler'], {
            'cmd': [
                '/data/data/com.termux/files/usr/bin/python3',
                '/data/data/com.termux/files/home/bin/suwayomi_daily_update.py',
                '--foreground'
            ]
        })
        self.assertEqual(list(rt_default.keys())[16], 'antra')
        self.assertEqual(rt_default['antra'], {
            'cmd': [
                '/data/data/com.termux/files/usr/bin/python3',
                '-u',
                '/data/data/com.termux/files/home/.local/lib/mobile-server/antra_worker.py'
            ]
        })

        rt_alt = service_catalog.runtime_services('/custom/home', '/custom/usr')
        self.assertEqual(len(rt_alt), 17)
        rt_alt_prefix = {k: rt_alt[k] for k in baseline_keys}
        self.assertEqual(rt_alt_prefix, char['supervisor_alt'])
        self.assertEqual(list(rt_alt.keys())[:15], list(char['supervisor_alt'].keys()))
        self.assertEqual(list(rt_alt.keys())[15], 'suwayomi_scheduler')
        self.assertEqual(rt_alt['suwayomi_scheduler'], {
            'cmd': [
                '/custom/usr/bin/python3',
                '/custom/home/bin/suwayomi_daily_update.py',
                '--foreground'
            ]
        })
        self.assertEqual(list(rt_alt.keys())[16], 'antra')
        self.assertEqual(rt_alt['antra'], {
            'cmd': [
                '/custom/usr/bin/python3',
                '-u',
                '/custom/home/.local/lib/mobile-server/antra_worker.py'
            ]
        })

        # Explicit order & count check
        services = service_catalog.runtime_services('/h', '/p')
        expected_keys = [
            'ssh', 'dashboard', 'caddy', 'filebrowser', 'transfer_gateway',
            'adguard', 'syncthing', 'vaultwarden', 'terminal', 'agy',
            'laptop_terminal', 'tor', 'suwayomi', 'suwayomi_filter', 'battery_guard',
            'suwayomi_scheduler', 'antra'
        ]
        self.assertEqual(list(services.keys()), expected_keys)
        self.assertEqual(len(services), 17)
        self.assertNotIn('port', services['suwayomi_scheduler'])
        self.assertNotIn('path', services['suwayomi_scheduler'])
        self.assertNotIn('protocol', services['suwayomi_scheduler'])
        self.assertNotIn('port', services['antra'])
        self.assertNotIn('path', services['antra'])
        self.assertNotIn('protocol', services['antra'])

    def test_dashboard_services_characterization_and_order(self):
        """Preserve all 9 dashboard cards, order, IDs, URLs, and verify absence of start_cmd/env."""
        char_file = ROOT / 'tests/fixtures/service-catalog-baseline.json'
        self.assertTrue(char_file.is_file(), f"Missing required baseline fixture: {char_file}")
        with open(char_file) as f:
            char = json.load(f)

        cards = service_catalog.dashboard_services()
        expected_keys = [
            'filebrowser', 'suwayomi', 'vaultwarden', 'adguard', 'terminal',
            'agy', 'converter', 'syncthing', 'battery_guard', 'antra'
        ]
        self.assertEqual(list(cards.keys()), expected_keys)
        self.assertEqual(list(cards.keys())[:9], list(char['dashboard_services_config'].keys()))
        self.assertEqual(len(cards), 10)

        for k in expected_keys[:9]:
            orig = dict(char['dashboard_services_config'][k])
            orig.pop('start_cmd', None)
            orig.pop('stop_cmd', None)
            orig.pop('env', None)
            self.assertEqual(cards[k], orig)

        self.assertEqual(cards['antra']['name'], 'Antra Music')
        self.assertEqual(cards['antra']['category'], 'Media')
        self.assertEqual(cards['antra']['open_url'], '/antra/')
        self.assertTrue(cards['antra']['can_toggle'])

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

        # Fallback probe ports helper
        self.assertEqual(service_catalog.suwayomi_probe_ports(), [4569, 4567])

    def test_filebrowser_single_source_endpoint_mutation(self):
        """Controlled mutation of filebrowser port updates runtime health, argv -p, dashboard card, and gateway backend argv."""
        orig_fb = copy.deepcopy(service_catalog.SERVICE_ENDPOINTS['filebrowser'])
        try:
            service_catalog.SERVICE_ENDPOINTS['filebrowser']['port'] = 18085
            rt = service_catalog.runtime_services('/home/test', '/usr/test')
            dash = service_catalog.dashboard_services()

            # FileBrowser runtime health port
            self.assertEqual(rt['filebrowser']['port'], 18085)
            # FileBrowser argv -p
            fb_cmd = rt['filebrowser']['cmd']
            p_idx = fb_cmd.index('-p')
            self.assertEqual(fb_cmd[p_idx + 1], '18085')

            # Dashboard card port
            self.assertEqual(dash['filebrowser']['port'], 18085)

            # transfer_gateway backend argv follows filebrowser port
            gw_cmd = rt['transfer_gateway']['cmd']
            backend_idx = gw_cmd.index('-backend')
            self.assertEqual(gw_cmd[backend_idx + 1], 'http://127.0.0.1:18085')
        finally:
            service_catalog.SERVICE_ENDPOINTS['filebrowser'] = orig_fb

    def test_supervisor_compatibility_wrapper(self):
        """server_supervisor.services() returns runtime_services with supervisor globals."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('server_supervisor', ROOT / 'scripts/server_supervisor.py')
        sup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sup)
        self.assertTrue(callable(sup.services))
        s = sup.services()
        self.assertEqual(len(s), 17)
        self.assertEqual(s['caddy']['port'], 8000)
        self.assertEqual(s['suwayomi_scheduler']['cmd'], [
            '/data/data/com.termux/files/usr/bin/python3',
            '/data/data/com.termux/files/home/bin/suwayomi_daily_update.py',
            '--foreground'
        ])
        self.assertEqual(s['antra']['cmd'], [
            '/data/data/com.termux/files/usr/bin/python3',
            '-u',
            '/data/data/com.termux/files/home/.local/lib/mobile-server/antra_worker.py'
        ])
        # Verify supervisor HOME and PREFIX globals remain unmodified
        self.assertEqual(sup.HOME, Path('/data/data/com.termux/files/home'))
        self.assertEqual(sup.PREFIX, Path('/data/data/com.termux/files/usr'))

    def test_installed_layout_bootstrap(self):
        """Supervisor and dashboard execute cleanly when installed to HOME/bin and PREFIX/bin with shared module in ~/.local/lib/mobile-server."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            fake_home = tmp / 'home'
            fake_prefix = tmp / 'usr'
            lib_dir = fake_home / '.local/lib/mobile-server'
            home_bin = fake_home / 'bin'
            prefix_bin = fake_prefix / 'bin'

            lib_dir.mkdir(parents=True, exist_ok=True)
            home_bin.mkdir(parents=True, exist_ok=True)
            prefix_bin.mkdir(parents=True, exist_ok=True)

            # Copy all flat app helpers so later modules integrate without a hardcoded incomplete list
            # Copy flat app helpers excluding dashboard.py (actual executable is PREFIX/bin/dashboard)
            for app_file in (ROOT / 'app').glob('*.py'):
                if app_file.name == 'dashboard.py':
                    continue
                shutil.copy(app_file, lib_dir / app_file.name)

            # Copy server_supervisor.py and dashboard
            shutil.copy(ROOT / 'scripts/server_supervisor.py', home_bin / 'server_supervisor.py')
            shutil.copy(ROOT / 'app/dashboard.py', prefix_bin / 'dashboard')

            child_code = '''
import sys, os, importlib.util, runpy
from pathlib import Path

td = Path(os.environ['TEMP_RUN_DIR'])
fake_home = td / 'home'
fake_prefix = td / 'usr'
lib_dir = fake_home / '.local/lib/mobile-server'
home_bin = fake_home / 'bin'
prefix_bin = fake_prefix / 'bin'

# Load installed telemetry.py via importlib spec, register sys.modules, mock start before runpy
spec = importlib.util.spec_from_file_location('telemetry', str(lib_dir / 'telemetry.py'))
telem_mod = importlib.util.module_from_spec(spec)
sys.modules['telemetry'] = telem_mod
spec.loader.exec_module(telem_mod)
telem_mod.Telemetry.start = lambda self: None

# Install audit hook blocking connect/Popen/kill/killpg and writes outside temp
def audit_hook(event, args):
    if event in ('socket.connect', 'socket.bind'):
        raise PermissionError(f'Blocked network: {event}')
    if event.startswith('subprocess.'):
        raise PermissionError(f'Blocked subprocess: {event}')
    if event in ('os.kill', 'os.killpg'):
        raise PermissionError(f'Blocked signal: {event}')
    if event == 'open' and len(args) > 1 and args[1] is not None:
        mode = str(args[1])
        if any(m in mode for m in ('w', 'a', '+', 'x')):
            p = str(args[0])
            if not p.startswith(str(td)):
                raise PermissionError(f'Blocked write outside temp: {p}')

sys.addaudithook(audit_hook)

# Run supervisor via runpy
sup_globals = runpy.run_path(str(home_bin / 'server_supervisor.py'), run_name='server_supervisor')
assert sup_globals['HOME'] == Path('/data/data/com.termux/files/home'), f"HOME modified: {sup_globals['HOME']}"
assert sup_globals['PREFIX'] == Path('/data/data/com.termux/files/usr'), f"PREFIX modified: {sup_globals['PREFIX']}"
sc = sys.modules['service_catalog']
assert Path(sc.__file__).resolve() == (lib_dir / 'service_catalog.py').resolve(), f"Wrong service_catalog: {sc.__file__}"

# Run dashboard via runpy
dash_globals = runpy.run_path(str(prefix_bin / 'dashboard'), run_name='dashboard')
assert len(dash_globals['SERVICES_CONFIG']) == 10
assert dash_globals['SERVICES_CONFIG']['filebrowser']['port'] == 8085
assert dash_globals['SERVICES_CONFIG']['antra']['open_url'] == '/antra/'
print('INSTALLED_BOOTSTRAP_OK')
'''
            child_runner = tmp / 'runner.py'
            child_runner.write_text(child_code)

            env = {
                'HOME': str(fake_home),
                'TEMP_RUN_DIR': str(tmp),
                'PATH': '/usr/bin:/bin',
                'TERMUX_HOME': '/hostile/home',
                'PREFIX': '/hostile/usr',
                'PYTHONDONTWRITEBYTECODE': '1'
            }

            res = subprocess.run(
                [sys.executable, '-I', str(child_runner)],
                cwd=str(tmp),
                env=env,
                capture_output=True,
                text=True,
                timeout=15
            )
            self.assertEqual(res.returncode, 0, f"Bootstrap smoke failed: {res.stderr}\n{res.stdout}")
            self.assertIn("INSTALLED_BOOTSTRAP_OK", res.stdout)


if __name__ == '__main__':
    unittest.main()
