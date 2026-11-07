"""
tests/test_service_panel.py: Unit tests for ServicePanel owner.
"""

from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
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


class ServicePanelBaseTestCase(unittest.TestCase):
    """Base class enforcing strict system call isolation across all test cases."""

    def tearDown(self):
        # Probe helpers catch exceptions, so guards must also check their counters.
        self.assertEqual(self.unmocked_check_output_calls, 0)
        self.assertEqual(self.unmocked_socket_calls, 0)
        self.assertEqual(self.unmocked_proc_open_calls, 0)
        super().tearDown()

    def setUp(self):
        super().setUp()
        # Default counters for trapped unmocked calls
        self.unmocked_check_output_calls = 0
        self.unmocked_socket_calls = 0
        self.unmocked_proc_open_calls = 0

        # Tripwire: guard subprocess.check_output
        orig_check_output = subprocess.check_output
        def guarded_check_output(*args, **kwargs):
            self.unmocked_check_output_calls += 1
            raise AssertionError(f"Tripwire: unmocked subprocess.check_output attempted: {args} {kwargs}")

        # Tripwire: guard socket.socket
        import socket
        def guarded_socket(*args, **kwargs):
            self.unmocked_socket_calls += 1
            raise AssertionError(f"Tripwire: unmocked socket.socket attempted: {args} {kwargs}")

        # Tripwire: guard builtins.open specifically for /proc
        orig_open = open
        def guarded_open(file, *args, **kwargs):
            str_path = str(file)
            if str_path.startswith("/proc"):
                self.unmocked_proc_open_calls += 1
                raise AssertionError(f"Tripwire: unmocked /proc access attempted: {str_path}")
            return orig_open(file, *args, **kwargs)

        p1 = patch("subprocess.check_output", side_effect=guarded_check_output)
        p2 = patch("socket.socket", side_effect=guarded_socket)
        p3 = patch("builtins.open", side_effect=guarded_open)

        self.mock_guarded_co = p1.start()
        self.mock_guarded_socket = p2.start()
        self.mock_guarded_open = p3.start()

        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(p3.stop)

        # Standard tempdir and files for service panel tests
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
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

    def make_panel(
        self,
        services_config=None,
        disabled_dir=None,
        status_file=None,
        supervisor_script=None,
        get_service_pids=None,
        get_process_rss=None,
        is_service_port_open=None,
        get_suwayomi_unread=None,
    ) -> ServicePanel:
        """Factory creating ServicePanel with fully-mocked, isolated defaults."""
        return ServicePanel(
            services_config=services_config if services_config is not None else self.fake_config,
            disabled_dir=disabled_dir or self.disabled_dir,
            status_file=status_file or self.status_file,
            supervisor_script=supervisor_script or "/data/data/com.termux/files/home/bin/server_supervisor.py",
            get_service_pids=get_service_pids if get_service_pids is not None else (lambda pat: []),
            get_process_rss=get_process_rss if get_process_rss is not None else (lambda pid: None),
            is_service_port_open=is_service_port_open if is_service_port_open is not None else (lambda port: False),
            get_suwayomi_unread=get_suwayomi_unread,
        )


class ServicePanelTests(ServicePanelBaseTestCase):
    def test_pure_constructor_no_side_effects(self):
        """Constructor performs no I/O, process calls, or network requests."""
        with patch("subprocess.run") as mock_run:
            panel = ServicePanel(
                services_config=self.fake_config,
                disabled_dir=self.disabled_dir,
                status_file=self.status_file,
            )
            self.assertEqual(mock_run.call_count, 0)
            self.assertEqual(self.unmocked_check_output_calls, 0)
            self.assertEqual(self.unmocked_socket_calls, 0)
            self.assertEqual(self.unmocked_proc_open_calls, 0)

    def test_ondemand_service_reporting(self):
        panel = self.make_panel(
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

        panel = self.make_panel(
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
        panel = self.make_panel(
            get_service_pids=lambda p: [],
            is_service_port_open=lambda p: False,
        )
        rep = panel.get_services_report()
        demo = next(s for s in rep["services"] if s["id"] == "demo")
        self.assertFalse(demo["running"])
        self.assertEqual(demo["status_badge"], "Sleeping (0 MB)")
        self.assertGreater(rep["sleeping_count"], 0)

    def test_running_healthy_and_memory_aggregation(self):
        panel = self.make_panel(
            services_config={"demo": self.fake_config["demo"]},
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
        panel = self.make_panel(
            services_config={"demo": self.fake_config["demo"]},
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

        panel = self.make_panel(
            services_config={"demo": self.fake_config["demo"]},
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

        panel = self.make_panel(
            services_config={"demo": self.fake_config["demo"]},
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

        panel = self.make_panel(
            services_config={"suwayomi": self.fake_config["suwayomi"]},
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
        panel = self.make_panel()
        res = panel.toggle_service("nonexistent")
        self.assertEqual(res, {"status": "error", "message": "Unknown service"})

    def test_toggle_ondemand_service_noop(self):
        panel = self.make_panel()
        res = panel.toggle_service("ondemand_svc")
        self.assertIn("services", res)

    @patch("subprocess.run")
    def test_toggle_service_invocation_exact_argv_and_inversion(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        panel = self.make_panel(
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

        # 4. Explicit enable=True override
        panel.toggle_service("demo", enable=True)
        mock_run.assert_called_with([
            "python3", "/test/bin/server_supervisor.py",
            "--service", "demo", "--enable", "true"
        ], capture_output=True, text=True, timeout=25)

    @patch("subprocess.run")
    def test_toggle_service_failure_returncode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        panel = self.make_panel()
        res = panel.toggle_service("demo", enable=True)
        self.assertEqual(res, {"status": "error", "message": "Service control failed; see server recovery logs"})

    @patch("subprocess.run", side_effect=OSError("Exec error"))
    def test_toggle_service_timeout_or_oserror(self, mock_run):
        panel = self.make_panel()
        res = panel.toggle_service("demo", enable=True)
        self.assertEqual(res, {"status": "error", "message": "Service control timed out; see server recovery logs"})

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["python3"], timeout=25))
    def test_toggle_service_subprocess_timeout_expired(self, mock_run):
        panel = self.make_panel()
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

        panel = self.make_panel(
            services_config={"demo": self.fake_config["demo"]},
            get_service_pids=fake_pids,
            get_process_rss=lambda pid: 10.0,
            is_service_port_open=lambda p: False,
        )
        res = panel.toggle_service("demo", enable=True)
        # 3 polls during toggle loop + 1 call during final get_services_report
        self.assertEqual(len(poll_calls), 4)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(0.1)

    @patch("time.sleep", return_value=None)
    @patch("subprocess.run")
    def test_toggle_enable_polls_exhaustion_15_times(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0)
        poll_calls = []

        def fake_pids(pattern):
            poll_calls.append(pattern)
            return []

        panel = self.make_panel(
            services_config={"demo": self.fake_config["demo"]},
            get_service_pids=fake_pids,
            get_process_rss=lambda pid: 10.0,
            is_service_port_open=lambda p: False,
        )
        res = panel.toggle_service("demo", enable=True)
        # 15 polls in toggle loop + 1 call during final get_services_report
        self.assertEqual(len(poll_calls), 16)
        self.assertEqual(mock_sleep.call_count, 15)
        mock_sleep.assert_called_with(0.1)

    @patch("subprocess.run")
    def test_toggle_laptop_terminal_and_agy_whitelisted(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        from service_catalog import dashboard_services
        cfg = dashboard_services()
        self.assertIn("laptop_terminal", cfg)
        self.assertIn("agy", cfg)
        panel = self.make_panel(
            services_config=cfg,
            supervisor_script="/test/bin/server_supervisor.py",
            get_service_pids=lambda p: [],
            is_service_port_open=lambda p: False,
        )

        # Toggle laptop_terminal explicitly off
        res1 = panel.toggle_service("laptop_terminal", enable=False)
        self.assertIn("services", res1)
        mock_run.assert_called_with([
            "python3", "/test/bin/server_supervisor.py",
            "--service", "laptop_terminal", "--enable", "false"
        ], capture_output=True, text=True, timeout=25)

        # Toggle agy explicitly on
        res2 = panel.toggle_service("agy", enable=True)
        self.assertIn("services", res2)
        mock_run.assert_called_with([
            "python3", "/test/bin/server_supervisor.py",
            "--service", "agy", "--enable", "true"
        ], capture_output=True, text=True, timeout=25)

        # Non-dashboard service like tor should be rejected
        res3 = panel.toggle_service("tor", enable=True)
        self.assertEqual(res3, {"status": "error", "message": "Unknown service"})


class ProbeHelpersTests(ServicePanelBaseTestCase):
    def test_probe_port_zero_or_none(self):
        self.assertFalse(probe_service_port_open(0))
        self.assertFalse(probe_service_port_open(None))
        self.assertFalse(probe_service_port_open(""))

    def test_probe_port_open_successful(self):
        mock_sock_inst = MagicMock()
        mock_sock_inst.connect_ex.return_value = 0
        with patch("socket.socket", return_value=mock_sock_inst):
            self.assertTrue(probe_service_port_open(8080))
            mock_sock_inst.settimeout.assert_called_with(0.25)
            mock_sock_inst.connect_ex.assert_called_with(("127.0.0.1", 8080))
            mock_sock_inst.close.assert_called_once()

    def test_probe_port_open_closed_or_error(self):
        mock_sock_inst = MagicMock()
        mock_sock_inst.connect_ex.return_value = 111  # ECONNREFUSED
        with patch("socket.socket", return_value=mock_sock_inst):
            self.assertFalse(probe_service_port_open(8080))

        with patch("socket.socket", side_effect=Exception("socket creation failed")):
            self.assertFalse(probe_service_port_open(8080))

    def test_probe_pids_error_empty_list(self):
        with patch("subprocess.check_output", side_effect=Exception("pgrep failed")):
            self.assertEqual(probe_service_pids("anything"), [])

    def test_probe_pids_excludes_self(self):
        with patch("subprocess.check_output", return_value=b"100 200\n"), \
             patch("os.getpid", return_value=100):
            self.assertEqual(probe_service_pids("pattern"), [200])

    def test_probe_rss_valid_file(self):
        proc_status = "Name:\tdemo\nState:\tS\nVmRSS:\t 10240 kB\n"
        from unittest.mock import mock_open
        with patch("builtins.open", mock_open(read_data=proc_status)) as m:
            rss = probe_process_rss(1234)
            self.assertEqual(rss, 10.0)
            m.assert_called_once_with("/proc/1234/status")

    def test_probe_rss_invalid_pid_or_error(self):
        with patch("builtins.open", side_effect=FileNotFoundError("No such process")):
            self.assertIsNone(probe_process_rss(-99999))


class TestIsolationTripwireCheck(ServicePanelBaseTestCase):
    def test_isolation_tripwires_verified(self):
        """Prove tripwires raise AssertionError if unmocked calls escape."""
        with self.assertRaises(AssertionError):
            subprocess.check_output(["echo", "1"])
        import socket
        with self.assertRaises(AssertionError):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self.assertRaises(AssertionError):
            open("/proc/1/status")
        self.assertEqual(
            (self.unmocked_check_output_calls, self.unmocked_socket_calls, self.unmocked_proc_open_calls),
            (1, 1, 1),
        )
        # These intentional guard checks are verified above; shared teardown checks accidental calls.
        self.unmocked_check_output_calls = 0
        self.unmocked_socket_calls = 0
        self.unmocked_proc_open_calls = 0
