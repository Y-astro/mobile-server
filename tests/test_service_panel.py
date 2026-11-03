"""
tests/test_service_panel.py: Unit tests for ServicePanel owner.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from service_panel import (
    ServicePanel,
    probe_process_rss,
    probe_service_pids,
    probe_service_port_open,
)


class ServicePanelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.disabled_dir = self.root / "services_disabled"
        self.disabled_dir.mkdir(parents=True)
        self.status_file = self.root / "status.json"

        self.fake_config = {
            "demo": {
                "name": "Demo Service",
                "pattern": "demo_binary",
                "port": 8080,
                "desc": "Demo service description",
                "category": "Test",
                "can_toggle": True,
            },
            "ondemand_svc": {
                "name": "Ondemand Service",
                "pattern": "ondemand_binary",
                "port": 9090,
                "desc": "Ondemand service description",
                "is_ondemand": True,
            },
            "suwayomi": {
                "name": "Suwayomi",
                "pattern": "suwayomi_binary",
                "port": 4567,
                "desc": "Suwayomi manga server",
                "can_toggle": True,
            },
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pure_constructor_no_side_effects(self):
        """Constructor performs no I/O, process calls, or network requests."""
        with patch("subprocess.run") as mock_run, \
             patch("subprocess.check_output") as mock_co, \
             patch("socket.socket") as mock_sock:
            panel = ServicePanel(
                services_config=self.fake_config,
                disabled_dir=self.disabled_dir,
                status_file=self.status_file,
            )
            self.assertEqual(mock_run.call_count, 0)
            self.assertEqual(mock_co.call_count, 0)
            self.assertEqual(mock_sock.call_count, 0)

    def test_ondemand_service_reporting(self):
        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=lambda p: [123],
        )
        rep = panel.get_services_report()
        ondemand_entries = [s for s in rep["services"] if s["id"] == "ondemand_svc"]
        self.assertEqual(len(ondemand_entries), 1)
        s = ondemand_entries[0]
        self.assertTrue(s["ondemand"])
        self.assertTrue(s["running"])
        self.assertIsNone(s["pid"])
        self.assertEqual(s["rss_mb"], 0.0)
        self.assertEqual(s["status_badge"], "Ready (0 MB Idle)")
        self.assertFalse(s["can_toggle"])
        self.assertTrue(s["memory_complete"])

    def test_disabled_service_receives_no_probes(self):
        (self.disabled_dir / "demo").touch()
        pid_calls = []
        port_calls = []

        def fake_pids(pattern):
            pid_calls.append(pattern)
            return [100]

        def fake_port(p):
            port_calls.append(p)
            return True

        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=fake_pids,
            is_service_port_open=fake_port,
        )
        rep = panel.get_services_report()
        demo = next(s for s in rep["services"] if s["id"] == "demo")
        self.assertFalse(demo["running"])
        self.assertEqual(demo["status_badge"], "Disabled (0 MB)")
        self.assertNotIn("demo_binary", pid_calls)
        self.assertNotIn(8080, port_calls)

    def test_sleeping_service_badge(self):
        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=lambda p: [],
            is_service_port_open=lambda p: False,
        )
        rep = panel.get_services_report()
        demo = next(s for s in rep["services"] if s["id"] == "demo")
        self.assertFalse(demo["running"])
        self.assertEqual(demo["status_badge"], "Sleeping (0 MB)")
        self.assertGreater(rep["sleeping_count"], 0)

    def test_running_healthy_and_memory_aggregation(self):
        panel = ServicePanel(
            services_config={"demo": self.fake_config["demo"]},
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=lambda p: [100, 101],
            get_process_rss=lambda pid: 12.5 if pid == 100 else 10.0,
            is_service_port_open=lambda p: True,
        )
        rep = panel.get_services_report()
        demo = rep["services"][0]
        self.assertTrue(demo["running"])
        self.assertEqual(demo["rss_mb"], 22.5)
        self.assertTrue(demo["memory_complete"])
        self.assertEqual(demo["status_badge"], "Active (22.5 MB)")
        self.assertEqual(rep["active_memory_mb"], 22.5)
        self.assertTrue(rep["memory_complete"])

    def test_running_unavailable_memory_marks_incomplete(self):
        panel = ServicePanel(
            services_config={"demo": self.fake_config["demo"]},
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=lambda p: [100, 101],
            get_process_rss=lambda pid: 12.5 if pid == 100 else None,
            is_service_port_open=lambda p: True,
        )
        rep = panel.get_services_report()
        demo = rep["services"][0]
        self.assertTrue(demo["running"])
        self.assertIsNone(demo["rss_mb"])
        self.assertFalse(demo["memory_complete"])
        self.assertEqual(demo["status_badge"], "Active (Memory Unavailable)")
        self.assertIsNone(rep["active_memory_mb"])
        self.assertFalse(rep["memory_complete"])

    def test_status_file_healthy_fallback(self):
        with open(self.status_file, "w") as f:
            json.dump({"services": {"demo": {"state": "healthy"}}}, f)

        panel = ServicePanel(
            services_config={"demo": self.fake_config["demo"]},
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=lambda p: [],
            is_service_port_open=lambda p: False,
        )
        rep = panel.get_services_report()
        demo = rep["services"][0]
        self.assertTrue(demo["running"])
        self.assertIsNone(demo["rss_mb"])
        self.assertEqual(demo["status_badge"], "Active (Memory Unavailable)")

    def test_status_file_json_error_handled_gracefully(self):
        with open(self.status_file, "w") as f:
            f.write("{invalid json")

        panel = ServicePanel(
            services_config={"demo": self.fake_config["demo"]},
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=lambda p: [],
            is_service_port_open=lambda p: False,
        )
        rep = panel.get_services_report()
        demo = rep["services"][0]
        self.assertFalse(demo["running"])

    def test_suwayomi_unread_callback_propagation(self):
        called_with = []

        def fake_unread(force=False):
            called_with.append(force)
            return {"unread_count": 42, "unread_titles": 3, "latest_unread": 5}

        panel = ServicePanel(
            services_config={"suwayomi": self.fake_config["suwayomi"]},
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=lambda p: [555],
            get_process_rss=lambda pid: 10.0,
            is_service_port_open=lambda p: True,
            get_suwayomi_unread=fake_unread,
        )
        rep = panel.get_services_report(force=True)
        self.assertEqual(called_with, [True])
        suw = rep["services"][0]
        self.assertEqual(suw["unread_updates"], 42)
        self.assertEqual(suw["unread_titles"], 3)
        self.assertEqual(suw["latest_unread"], 5)

    def test_toggle_unknown_service(self):
        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
        )
        res = panel.toggle_service("nonexistent")
        self.assertEqual(res, {"status": "error", "message": "Unknown service"})

    def test_toggle_ondemand_service_noop(self):
        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
        )
        res = panel.toggle_service("ondemand_svc")
        self.assertIn("services", res)

    @patch("subprocess.run")
    def test_toggle_service_invocation_exact_argv_and_inversion(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            supervisor_script="/test/bin/server_supervisor.py",
            get_service_pids=lambda p: [123],
        )

        # 1. Currently active (not disabled) -> toggle with enable=None targets False
        panel.toggle_service("demo", enable=None)
        mock_run.assert_called_with([
            "python3", "/test/bin/server_supervisor.py",
            "--service", "demo", "--enable", "false"
        ], capture_output=True, text=True, timeout=25)

        # 2. Currently disabled -> toggle with enable=None targets True
        (self.disabled_dir / "demo").touch()
        panel.toggle_service("demo", enable=None)
        mock_run.assert_called_with([
            "python3", "/test/bin/server_supervisor.py",
            "--service", "demo", "--enable", "true"
        ], capture_output=True, text=True, timeout=25)

        # 3. Explicit enable=False override
        panel.toggle_service("demo", enable=False)
        mock_run.assert_called_with([
            "python3", "/test/bin/server_supervisor.py",
            "--service", "demo", "--enable", "false"
        ], capture_output=True, text=True, timeout=25)

    @patch("subprocess.run")
    def test_toggle_service_failure_returncode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
        )
        res = panel.toggle_service("demo", enable=True)
        self.assertEqual(res, {"status": "error", "message": "Service control failed; see server recovery logs"})

    @patch("subprocess.run", side_effect=OSError("Exec error"))
    def test_toggle_service_timeout_or_oserror(self, mock_run):
        panel = ServicePanel(
            services_config=self.fake_config,
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
        )
        res = panel.toggle_service("demo", enable=True)
        self.assertEqual(res, {"status": "error", "message": "Service control timed out; see server recovery logs"})

    @patch("time.sleep", return_value=None)
    @patch("subprocess.run")
    def test_toggle_enable_polls_pid(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0)
        poll_calls = []

        def fake_pids(pattern):
            poll_calls.append(pattern)
            # return pid only on third poll
            if len(poll_calls) >= 3:
                return [999]
            return []

        panel = ServicePanel(
            services_config={"demo": self.fake_config["demo"]},
            disabled_dir=self.disabled_dir,
            status_file=self.status_file,
            get_service_pids=fake_pids,
            get_process_rss=lambda pid: 10.0,
            is_service_port_open=lambda p: False,
        )
        res = panel.toggle_service("demo", enable=True)
        # 3 polls during toggle loop + 1 call during final get_services_report
        self.assertEqual(len(poll_calls), 4)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(0.1)


class ProbeHelpersTests(unittest.TestCase):
    def test_probe_port_zero_or_none(self):
        self.assertFalse(probe_service_port_open(0))
        self.assertFalse(probe_service_port_open(None))
        self.assertFalse(probe_service_port_open(""))

    @patch("subprocess.check_output", side_effect=Exception("pgrep failed"))
    def test_probe_pids_error_empty_list(self, mock_co):
        self.assertEqual(probe_service_pids("anything"), [])

    @patch("subprocess.check_output", return_value=b"100 200\n")
    @patch("os.getpid", return_value=100)
    def test_probe_pids_excludes_self(self, mock_getpid, mock_co):
        self.assertEqual(probe_service_pids("pattern"), [200])

    def test_probe_rss_invalid_pid(self):
        self.assertIsNone(probe_process_rss(-99999))
