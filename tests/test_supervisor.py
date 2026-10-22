import contextlib
import http.server
import importlib.util
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

FILE = Path(__file__).parents[1] / 'scripts/server_supervisor.py'
spec = importlib.util.spec_from_file_location('supervisor', FILE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {'STATE': root/'state', 'DISABLED': root/'disabled', 'PAUSED': root/'state/paused', 'PUBLIC_PAUSED': root/'public-paused'}.items():
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)
        m.STATE.mkdir()
        m.DISABLED.mkdir()
        for name, value in {'services': {'fake': {'cmd': ['false']}}, 'healthy': False, 'matching_processes': [], 'launch': Mock(), 'terminate': None}.items():
            p = patch.object(m, name, return_value=value)
            mock = p.start()
            setattr(self, name, mock)
            self.addCleanup(p.stop)

    def test_disabled_service_is_never_probed_or_started(self):
        (m.DISABLED / 'fake').touch()
        m.Supervisor().cycle(initial=True)
        self.healthy.assert_not_called()
        self.launch.assert_not_called()

    def test_three_failed_checks_then_backoff(self):
        s = m.Supervisor()
        s.cycle(); s.cycle()
        self.launch.assert_not_called()
        s.cycle()
        self.launch.assert_called_once()
        for _ in range(8):
            s.cycle()
        self.launch.assert_called_once()
        s.history['fake']['next_start'] = 0
        s.cycle()
        self.assertEqual(self.launch.call_count, 2)

    def test_initial_ensure_does_not_duplicate_hung_process(self):
        self.matching_processes.return_value = [(123, 'identity')]
        m.Supervisor().cycle(initial=True)
        self.launch.assert_not_called()

    def test_pause_blocks_automatic_start(self):
        m.PAUSED.touch()
        s = m.Supervisor()
        for _ in range(4):
            s.cycle()
        self.launch.assert_not_called()

    def test_healthy_check_resets_failure_threshold(self):
        s = m.Supervisor()
        s.cycle(); s.cycle()
        self.healthy.return_value = True
        s.cycle()
        self.healthy.return_value = False
        s.cycle(); s.cycle()
        self.launch.assert_not_called()

    def test_kernel_lock_rejects_second_owner(self):
        with m.lock('test'):
            with self.assertRaises(BlockingIOError):
                with m.lock('test', blocking=False):
                    pass

    def test_control_lock_wait_is_bounded(self):
        with m.lock('test'):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                with m.lock('test', timeout=.05):
                    pass
            self.assertLess(time.monotonic()-start, .5)

    def test_disable_records_intent_even_when_service_is_already_down(self):
        m.control('fake', False)
        self.assertTrue((m.DISABLED/'fake').exists())
        self.launch.assert_not_called()

    def test_enable_cannot_override_server_pause(self):
        m.PAUSED.touch()
        with self.assertRaises(RuntimeError):
            m.control('fake', True)
        self.launch.assert_not_called()

    def test_backoff_is_capped(self):
        s = m.Supervisor()
        for _ in range(20):
            s.history.setdefault('fake', dict(failures=2, retries=0, next_start=0))
            s.history['fake']['next_start'] = 0
            s.history['fake']['failures'] = 2
            s.cycle()
        self.assertLessEqual(s.history['fake']['next_start']-time.monotonic(), 300)


class HealthTests(unittest.TestCase):
    def test_http_500_is_unhealthy_and_200_is_healthy(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500 if self.path == '/broken' else 502 if self.path == '/upstream-down' else 200)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            self.assertFalse(m.healthy('test', {'port': server.server_port, 'path': '/broken'}))
            self.assertTrue(m.healthy('test', {'port': server.server_port, 'path': '/ok'}))
            self.assertTrue(m.healthy('caddy', {'port': server.server_port, 'path': '/upstream-down'}))
            self.assertFalse(m.healthy('dashboard', {'port': server.server_port, 'path': '/upstream-down'}))
        finally:
            server.shutdown(); server.server_close(); t.join()

    def test_open_port_without_http_response_is_unhealthy(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            start = time.monotonic()
            self.assertFalse(m.healthy('test', {'port': server.getsockname()[1], 'path': '/'}))
            self.assertLess(time.monotonic()-start, 3)


if __name__ == '__main__':
    unittest.main()
