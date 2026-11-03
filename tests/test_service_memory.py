"""Unit and integration tests for truthful service memory telemetry."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from auth import hash_password, save_users


class ServiceMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.oldhome = os.environ.get('HOME')
        os.environ['HOME'] = cls.temp.name
        save_users(
            Path(cls.temp.name) / '.config/dashboard/users.json',
            {'testadmin': {'password_hash': hash_password('test-password-long'), 'role': 'admin'}}
        )
        spec = importlib.util.spec_from_file_location(
            'isolated_dashboard_mem',
            Path(__file__).resolve().parents[1] / 'app/dashboard.py'
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()
        if cls.oldhome is not None:
            os.environ['HOME'] = cls.oldhome

    def test_no_synthetic_jitter_or_estimates_exist(self):
        self.assertFalse(hasattr(self.mod, 'ESTIMATED_SERVICE_MEM'))
        self.assertFalse(hasattr(self.mod, 'get_dynamic_service_rss'))

    def test_full_measured_rss_aggregation(self):
        with patch.object(self.mod, 'is_service_port_open', return_value=False), \
             patch.object(self.mod, 'get_service_pids', side_effect=lambda pattern: [1000]), \
             patch.object(self.mod, 'get_process_rss', return_value=15.0), \
             patch('pathlib.Path.exists', return_value=False):
            report = self.mod.get_services_report()
            self.assertTrue(report['memory_complete'])
            self.assertIsNotNone(report['active_memory_mb'])
            self.assertIsNotNone(report['total_active_mem_mb'])
            self.assertEqual(report['active_memory_mb'], report['total_active_mem_mb'])

            active_count = 0
            for s in report['services']:
                if s.get('ondemand'):
                    self.assertEqual(s['rss_mb'], 0.0)
                    self.assertTrue(s['memory_complete'])
                    self.assertEqual(s['status_badge'], 'Ready (0 MB Idle)')
                else:
                    self.assertEqual(s['rss_mb'], 15.0)
                    self.assertTrue(s['memory_complete'])
                    self.assertEqual(s['status_badge'], 'Active (15.0 MB)')
                    active_count += 1

            expected_total = round(active_count * 15.0, 1)
            self.assertEqual(report['active_memory_mb'], expected_total)

    def test_measured_zero_rss_remains_numeric_zero(self):
        with patch.object(self.mod, 'is_service_port_open', return_value=False), \
             patch.object(self.mod, 'get_service_pids', side_effect=lambda pattern: [1000]), \
             patch.object(self.mod, 'get_process_rss', return_value=0.0), \
             patch('pathlib.Path.exists', return_value=False):
            report = self.mod.get_services_report()
            self.assertTrue(report['memory_complete'])
            self.assertEqual(report['active_memory_mb'], 0.0)
            self.assertEqual(report['total_active_mem_mb'], 0.0)
            for s in report['services']:
                if not s.get('ondemand'):
                    self.assertEqual(s['rss_mb'], 0.0)
                    self.assertTrue(s['memory_complete'])
                    self.assertEqual(s['status_badge'], 'Active (0.0 MB)')

    def test_no_readable_rss_for_running_healthy_service(self):
        with patch.object(self.mod, 'is_service_port_open', return_value=False), \
             patch.object(self.mod, 'get_service_pids', side_effect=lambda pattern: [2000]), \
             patch.object(self.mod, 'get_process_rss', return_value=None), \
             patch('pathlib.Path.exists', return_value=False):
            report = self.mod.get_services_report()
            self.assertFalse(report['memory_complete'])
            self.assertIsNone(report['active_memory_mb'])
            self.assertIsNone(report['total_active_mem_mb'])
            for s in report['services']:
                if not s.get('ondemand'):
                    self.assertIsNone(s['rss_mb'])
                    self.assertFalse(s['memory_complete'])
                    self.assertEqual(s['status_badge'], 'Active (Memory Unavailable)')

    def test_mixed_known_and_unknown_pids(self):
        def custom_pids(pattern):
            if 'filebrowser' in pattern:
                return [101, 102]
            return [201]

        def custom_rss(pid):
            if pid == 101:
                return 20.0
            if pid == 102:
                return None
            return 10.0

        with patch.object(self.mod, 'is_service_port_open', return_value=False), \
             patch.object(self.mod, 'get_service_pids', side_effect=custom_pids), \
             patch.object(self.mod, 'get_process_rss', side_effect=custom_rss), \
             patch('pathlib.Path.exists', return_value=False):
            report = self.mod.get_services_report()
            self.assertFalse(report['memory_complete'])
            self.assertIsNone(report['active_memory_mb'])
            self.assertIsNone(report['total_active_mem_mb'])

            fb = next(s for s in report['services'] if s['id'] == 'filebrowser')
            self.assertIsNone(fb['rss_mb'])
            self.assertFalse(fb['memory_complete'])
            self.assertEqual(fb['status_badge'], 'Active (Memory Unavailable)')

            vw = next(s for s in report['services'] if s['id'] == 'vaultwarden')
            self.assertEqual(vw['rss_mb'], 10.0)
            self.assertTrue(vw['memory_complete'])
            self.assertEqual(vw['status_badge'], 'Active (10.0 MB)')

    def test_disabled_service_zero_behavior(self):
        orig_exists = Path.exists

        def fake_exists(self):
            p = str(self)
            if 'services_disabled/filebrowser' in p:
                return True
            if 'services_disabled' in p:
                return False
            if 'status.json' in p:
                return False
            return False

        with patch.object(self.mod, 'is_service_port_open', return_value=False), \
             patch.object(self.mod, 'get_service_pids', return_value=[]), \
             patch.object(Path, 'exists', new=fake_exists):
            report = self.mod.get_services_report()
            fb = next(s for s in report['services'] if s['id'] == 'filebrowser')
            self.assertFalse(fb['running'])
            self.assertEqual(fb['rss_mb'], 0.0)
            self.assertTrue(fb['memory_complete'])
            self.assertEqual(fb['status_badge'], 'Disabled (0 MB)')

    def test_sleeping_service_zero_behavior(self):
        with patch.object(self.mod, 'is_service_port_open', return_value=False), \
             patch.object(self.mod, 'get_service_pids', return_value=[]), \
             patch('pathlib.Path.exists', return_value=False):
            report = self.mod.get_services_report()
            for s in report['services']:
                if not s.get('ondemand'):
                    self.assertFalse(s['running'])
                    self.assertEqual(s['rss_mb'], 0.0)
                    self.assertTrue(s['memory_complete'])
                    self.assertEqual(s['status_badge'], 'Sleeping (0 MB)')

    def test_converter_on_demand_zero_idle(self):
        report = self.mod.get_services_report()
        conv = next(s for s in report['services'] if s['id'] == 'converter')
        self.assertTrue(conv['running'])
        self.assertTrue(conv['ondemand'])
        self.assertEqual(conv['rss_mb'], 0.0)
        self.assertTrue(conv['memory_complete'])
        self.assertEqual(conv['status_badge'], 'Ready (0 MB Idle)')


if __name__ == '__main__':
    unittest.main()
