"""
tests/test_agy_sessions.py: Unit tests for AgySessions manager.
"""

import os
import subprocess
import threading
import unittest
from unittest.mock import ANY, MagicMock, call, patch

from app.agy_sessions import AgySessions


class TestAgySessions(unittest.TestCase):

    def setUp(self):
        self.prefix = "/custom/prefix"
        self.tmux_bin = f"{self.prefix}/bin/tmux"
        self.manager = AgySessions(prefix=self.prefix, tmux_bin=self.tmux_bin)

    def test_constructor_no_io_or_workers(self):
        """Constructor must not trigger filesystem, subprocess, or network calls."""
        with patch("subprocess.run") as mock_run, patch("glob.glob") as mock_glob, patch("os.path.exists") as mock_exists:
            mgr = AgySessions("/test", "/test/bin/tmux")
            self.assertEqual(mgr.prefix, "/test")
            self.assertEqual(mgr.tmux_bin, "/test/bin/tmux")
            mock_run.assert_not_called()
            mock_glob.assert_not_called()
            mock_exists.assert_not_called()

    @patch("glob.glob")
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_get_tmux_cmd_termux_socket_probe_success(self, mock_run, mock_exists, mock_glob):
        """Discovers Termux socket first when list-sessions succeeds."""
        mock_glob.side_effect = lambda pat: (
            ["/data/data/com.termux/files/usr/var/run/tmux-1000/default"]
            if "com.termux" in pat
            else ["/tmp/tmux-1000/default"]
        )
        mock_exists.return_value = True
        mock_run.return_value = MagicMock(returncode=0)

        cmd = self.manager.get_tmux_cmd()

        self.assertEqual(cmd, [self.tmux_bin, "-S", "/data/data/com.termux/files/usr/var/run/tmux-1000/default"])
        mock_run.assert_called_once_with(
            [self.tmux_bin, "-S", "/data/data/com.termux/files/usr/var/run/tmux-1000/default", "list-sessions"],
            capture_output=True,
            text=True,
            timeout=1,
        )

    @patch("glob.glob")
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_get_tmux_cmd_tmp_socket_probe_fallback(self, mock_run, mock_exists, mock_glob):
        """Probes /tmp socket when Termux socket fails list-sessions."""
        mock_glob.side_effect = lambda pat: (
            ["/data/data/com.termux/files/usr/var/run/tmux-1000/default"]
            if "com.termux" in pat
            else ["/tmp/tmux-1000/default"]
        )
        mock_exists.return_value = True
        # First call fails (returncode 1), second succeeds (returncode 0)
        mock_run.side_effect = [
            MagicMock(returncode=1),
            MagicMock(returncode=0),
        ]

        cmd = self.manager.get_tmux_cmd()

        self.assertEqual(cmd, [self.tmux_bin, "-S", "/tmp/tmux-1000/default"])
        self.assertEqual(mock_run.call_count, 2)

    @patch("glob.glob")
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_get_tmux_cmd_fallback_to_bin_on_error(self, mock_run, mock_exists, mock_glob):
        """Falls back to TMUX_BIN when sockets fail or raise exceptions."""
        mock_glob.side_effect = lambda pat: ["/tmp/tmux-1000/default"]
        mock_exists.return_value = True
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=1)

        cmd = self.manager.get_tmux_cmd()

        self.assertEqual(cmd, [self.tmux_bin])

    @patch.object(AgySessions, "get_tmux_cmd", return_value=["/custom/prefix/bin/tmux"])
    @patch("subprocess.run")
    def test_list_agy_sessions_primary_ordering_and_filtering(self, mock_run, mock_get_tmux):
        """Lists sessions with default primary first and non-agy sessions filtered out."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="other-session\nagy-inst2\nagy\nagy-inst1\nrandom\n",
        )

        sessions = self.manager.list_agy_sessions()

        mock_run.assert_called_once_with(
            ["/custom/prefix/bin/tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(
            sessions,
            [
                {"id": "default", "name": "AGY (Primary)", "is_primary": True},
                {"id": "inst2", "name": "AGY Instance inst2", "is_primary": False},
                {"id": "inst1", "name": "AGY Instance inst1", "is_primary": False},
            ],
        )

    @patch.object(AgySessions, "get_tmux_cmd", return_value=["/custom/prefix/bin/tmux"])
    @patch("subprocess.run")
    def test_list_agy_sessions_error_or_nonzero_returns_empty(self, mock_run, mock_get_tmux):
        """Returns empty list on nonzero exit or subprocess exception."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        self.assertEqual(self.manager.list_agy_sessions(), [])

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=3)
        self.assertEqual(self.manager.list_agy_sessions(), [])

    @patch.object(AgySessions, "list_agy_sessions")
    @patch.object(AgySessions, "get_tmux_cmd", return_value=["/custom/prefix/bin/tmux"])
    @patch("subprocess.run")
    def test_create_agy_session_new_session_argv_env_timeout(self, mock_run, mock_get_tmux, mock_list):
        """Creates session with exact argv, sanitized identifier, env, and timeouts."""
        mock_list.return_value = [{"id": "default", "name": "AGY (Primary)", "is_primary": True}]
        # has-session check returns nonzero (not found)
        mock_run.side_effect = [
            MagicMock(returncode=1),  # has-session
            MagicMock(returncode=0),  # new-session
        ]

        ident = self.manager.create_agy_session("valid_id-123!@#")

        self.assertEqual(ident, "valid_id-123")
        self.assertEqual(mock_run.call_count, 2)
        # Verify has-session timeout=3 fix
        mock_run.assert_has_calls([
            call(
                ["/custom/prefix/bin/tmux", "has-session", "-t", "=agy-valid_id-123"],
                capture_output=True,
                timeout=3,
            ),
            call(
                [
                    "/custom/prefix/bin/tmux",
                    "new-session",
                    "-d",
                    "-s",
                    "agy-valid_id-123",
                    "-n",
                    "agy",
                    f"{self.prefix}/bin/proot-distro login debian -- /usr/local/bin/agy-runner-loop",
                ],
                check=True,
                timeout=5,
                env=ANY,
            ),
        ])

        env = mock_run.call_args_list[1].kwargs["env"]
        self.assertEqual(env["TERM"], "xterm-256color")
        self.assertEqual(env["TMPDIR"], f"{self.prefix}/tmp")
        self.assertTrue(env["PATH"].startswith(f"{self.prefix}/bin:{self.prefix}/bin/applets:"))

    @patch.object(AgySessions, "list_agy_sessions")
    @patch.object(AgySessions, "get_tmux_cmd", return_value=["/custom/prefix/bin/tmux"])
    @patch("subprocess.run")
    def test_create_agy_session_existing_reuse_no_new_session(self, mock_run, mock_get_tmux, mock_list):
        """Reuses existing session if has-session succeeds (returncode 0)."""
        mock_list.return_value = []
        mock_run.return_value = MagicMock(returncode=0)

        ident = self.manager.create_agy_session("my-inst")

        self.assertEqual(ident, "my-inst")
        mock_run.assert_called_once_with(
            ["/custom/prefix/bin/tmux", "has-session", "-t", "=agy-my-inst"],
            capture_output=True,
            timeout=3,
        )

    @patch.object(AgySessions, "list_agy_sessions")
    def test_create_agy_session_max_instances_reached(self, mock_list):
        """Rejects create when current instances >= 8."""
        mock_list.return_value = [{"id": f"inst{i}"} for i in range(8)]
        with self.assertRaises(ValueError) as ctx:
            self.manager.create_agy_session("overflow")
        self.assertIn("Maximum of 8 AGY instances reached", str(ctx.exception))

    def test_create_agy_session_concurrency_serialization(self):
        """Concurrent creates cannot exceed max 8 instances due to in-memory lock."""
        shared_sessions = [{"id": "default", "name": "AGY (Primary)", "is_primary": True}]
        barrier = threading.Barrier(4)

        def fake_list():
            return list(shared_sessions)

        def fake_run(cmd, *args, **kwargs):
            if "has-session" in cmd:
                return MagicMock(returncode=1)
            elif "new-session" in cmd:
                # Extract session_name
                s_idx = cmd.index("-s")
                s_name = cmd[s_idx + 1]
                shared_sessions.append({"id": s_name[4:], "name": s_name, "is_primary": False})
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        with patch.object(self.manager, "list_agy_sessions", side_effect=fake_list), \
             patch.object(self.manager, "get_tmux_cmd", return_value=["tmux"]), \
             patch("subprocess.run", side_effect=fake_run):

            errors = []
            created = []

            def worker(ident):
                barrier.wait()
                try:
                    res = self.manager.create_agy_session(ident)
                    created.append(res)
                except ValueError as e:
                    errors.append(str(e))

            # Fill up to 7 existing sessions
            for i in range(1, 7):
                shared_sessions.append({"id": f"existing-{i}", "name": f"agy-existing-{i}", "is_primary": False})
            self.assertEqual(len(shared_sessions), 7)

            # 4 concurrent threads attempting to create when only 1 slot remains
            threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Exactly 1 should have succeeded, bringing total to 8; 3 should have failed with ValueError
            self.assertEqual(len(created), 1)
            self.assertEqual(len(errors), 3)
            self.assertEqual(len(shared_sessions), 8)
            for err in errors:
                self.assertIn("Maximum of 8 AGY instances reached", err)

    @patch.object(AgySessions, "get_tmux_cmd", return_value=["/custom/prefix/bin/tmux"])
    @patch("subprocess.run")
    def test_close_agy_session_secondary(self, mock_run, mock_get_tmux):
        """Closes secondary session with kill-session timeout=5."""
        res = self.manager.close_agy_session("worker-1")

        self.assertTrue(res)
        mock_run.assert_called_once_with(
            ["/custom/prefix/bin/tmux", "kill-session", "-t", "=agy-worker-1"],
            capture_output=True,
            timeout=5,
        )

    def test_close_agy_session_refuses_primary(self):
        """Refuses to close default, agy, or empty identifier."""
        for ident in ("default", "agy", "", "   ", "???"):
            with self.assertRaises(ValueError) as ctx:
                self.manager.close_agy_session(ident)
            self.assertIn("Primary AGY session cannot be terminated", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
