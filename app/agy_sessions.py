"""
app/agy_sessions.py: Multi-instance AGY session management via tmux.
"""

import glob
import os
import re
import secrets
import subprocess
import threading
from typing import Any, Dict, List, Optional


class AgySessions:
    """Manages AGY tmux sessions and command selection."""

    def __init__(self, prefix: str = "/data/data/com.termux/files/usr", tmux_bin: Optional[str] = None):
        self.prefix = prefix
        self.tmux_bin = tmux_bin or f"{prefix}/bin/tmux"
        self._create_lock = threading.Lock()

    def get_tmux_cmd(self) -> List[str]:
        for sock in glob.glob('/data/data/com.termux/files/usr/var/run/tmux-*/default') + glob.glob('/tmp/tmux-*/default'):
            if os.path.exists(sock):
                try:
                    res = subprocess.run([self.tmux_bin, '-S', sock, 'list-sessions'], capture_output=True, text=True, timeout=1)
                    if res.returncode == 0:
                        return [self.tmux_bin, '-S', sock]
                except Exception:
                    pass
        return [self.tmux_bin]

    def list_agy_sessions(self) -> List[Dict[str, Any]]:
        try:
            cmd = self.get_tmux_cmd() + ["list-sessions", "-F", "#{session_name}"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if res.returncode != 0:
                return []
            sessions = []
            has_primary = False
            for line in res.stdout.splitlines():
                s = line.strip()
                if s == "agy":
                    has_primary = True
                elif s.startswith("agy-"):
                    ident = s[4:]
                    sessions.append({"id": ident, "name": f"AGY Instance {ident}", "is_primary": False})
            if has_primary:
                sessions.insert(0, {"id": "default", "name": "AGY (Primary)", "is_primary": True})
            return sessions
        except Exception:
            return []

    def create_agy_session(self, ident: Optional[str] = None) -> str:
        with self._create_lock:
            if not ident:
                ident = secrets.token_hex(4)
            ident = re.sub(r'[^a-zA-Z0-9_-]', '', ident)[:32]
            if not ident or ident == "default":
                ident = secrets.token_hex(4)
            session_name = f"agy-{ident}"
            current = self.list_agy_sessions()
            if len(current) >= 8:
                raise ValueError("Maximum of 8 AGY instances reached")
            tmux = self.get_tmux_cmd()
            check = subprocess.run(tmux + ["has-session", "-t", f"={session_name}"], capture_output=True, timeout=3)
            if check.returncode != 0:
                cmd = f"{self.prefix}/bin/proot-distro login debian -- /usr/local/bin/agy-runner-loop"
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["TMPDIR"] = f"{self.prefix}/tmp"
                env["PATH"] = f"{self.prefix}/bin:{self.prefix}/bin/applets:" + env.get("PATH", "")
                subprocess.run(
                    tmux + ["new-session", "-d", "-s", session_name, "-n", "agy", cmd],
                    check=True, timeout=5, env=env
                )
            return ident

    def close_agy_session(self, ident: str) -> bool:
        ident = re.sub(r'[^a-zA-Z0-9_-]', '', ident)[:32]
        if not ident or ident in ("default", "agy"):
            raise ValueError("Primary AGY session cannot be terminated")
        session_name = f"agy-{ident}"
        tmux = self.get_tmux_cmd()
        subprocess.run(tmux + ["kill-session", "-t", f"={session_name}"], capture_output=True, timeout=5)
        return True
