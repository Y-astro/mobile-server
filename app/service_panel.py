"""
app/service_panel.py: Service reporting and control owner.
"""

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Dict, List, Optional

from service_catalog import suwayomi_probe_ports


def probe_service_pids(pattern: str) -> List[int]:
    """Find process IDs matching pattern, excluding current process."""
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern]).decode().strip()
        my_pid = os.getpid()
        return [int(p) for p in out.split() if int(p) != my_pid]
    except Exception:
        return []


def probe_process_rss(pid: int) -> Optional[float]:
    """Read VmRSS in MB from /proc/<pid>/status."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None


def probe_service_port_open(port: Any) -> bool:
    """Check if TCP port on localhost is listening."""
    if not port:
        return False
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        res = s.connect_ex(("127.0.0.1", int(port)))
        s.close()
        return res == 0
    except Exception:
        return False


class ServicePanel:
    """Manages service state reporting and lifecycle toggle commands."""

    def __init__(
        self,
        services_config: Optional[Mapping[str, Dict[str, Any]]] = None,
        disabled_dir: Optional[Path] = None,
        status_file: Optional[Path] = None,
        supervisor_script: Optional[str] = None,
        get_service_pids: Optional[Callable[[str], List[int]]] = None,
        get_process_rss: Optional[Callable[[int], Optional[float]]] = None,
        is_service_port_open: Optional[Callable[[Any], bool]] = None,
        get_suwayomi_unread: Optional[Callable[..., Dict[str, Any]]] = None,
    ):
        self._services_config = services_config
        self.disabled_dir = disabled_dir or Path("/data/data/com.termux/files/home/.config/services_disabled")
        self.status_file = status_file or Path("/data/data/com.termux/files/home/.config/server_recovery/status.json")
        self.supervisor_script = supervisor_script or os.path.expanduser("~/bin/server_supervisor.py")
        self._get_service_pids = get_service_pids or probe_service_pids
        self._get_process_rss = get_process_rss or probe_process_rss
        self._is_service_port_open = is_service_port_open or probe_service_port_open
        self._get_suwayomi_unread = get_suwayomi_unread

    def _resolve_config(self, dynamic_config: Optional[Mapping[str, Dict[str, Any]]] = None) -> Mapping[str, Dict[str, Any]]:
        if dynamic_config is not None:
            return dynamic_config
        if self._services_config is not None:
            return self._services_config
        from service_catalog import dashboard_services
        return dashboard_services()

    def get_services_report(
        self,
        force: bool = False,
        services_config: Optional[Mapping[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Generate services status and memory report."""
        config = self._resolve_config(services_config)
        services = []
        total_active_mem = 0.0
        services_sleeping = 0
        all_active_complete = True

        sup_status = {}
        if self.status_file.exists():
            try:
                with open(self.status_file) as f:
                    sup_status = json.load(f).get("services", {})
            except Exception:
                pass

        for sid, cfg in config.items():
            if cfg.get("is_ondemand"):
                services.append({
                    "id": sid,
                    "name": cfg["name"],
                    "category": cfg.get("category", "General"),
                    "running": True,
                    "ondemand": True,
                    "pid": None,
                    "rss_mb": 0.0,
                    "status_badge": "Ready (0 MB Idle)",
                    "port": cfg.get("port"),
                    "open_url": cfg.get("open_url"),
                    "open_label": cfg.get("open_label"),
                    "desc": cfg["desc"],
                    "can_toggle": False,
                    "memory_complete": True,
                })
                continue

            is_disabled = (self.disabled_dir / sid).exists()
            pids = self._get_service_pids(cfg["pattern"]) if not is_disabled else []
            sup_info = sup_status.get(sid, {})
            is_healthy = sup_info.get("state") == "healthy"

            # Check port listening state for network services
            port_listening = False
            if not is_disabled:
                check_ports = []
                if cfg.get("port"):
                    check_ports.append(cfg["port"])
                if sid == "suwayomi":
                    check_ports.extend(suwayomi_probe_ports())
                for cp in check_ports:
                    if self._is_service_port_open(cp):
                        port_listening = True
                        break

            running = (len(pids) > 0) or port_listening or (not is_disabled and is_healthy)

            rss_mb = 0.0
            service_memory_complete = True
            if running:
                if pids:
                    rss_values = [self._get_process_rss(pid) for pid in pids]
                    if all(v is not None for v in rss_values):
                        rss_mb = round(sum(rss_values), 1)
                        service_memory_complete = True
                        total_active_mem += rss_mb
                        badge = f"Active ({rss_mb} MB)"
                    else:
                        rss_mb = None
                        service_memory_complete = False
                        all_active_complete = False
                        badge = "Active (Memory Unavailable)"
                else:
                    rss_mb = None
                    service_memory_complete = False
                    all_active_complete = False
                    badge = "Active (Memory Unavailable)"
            else:
                services_sleeping += 1
                if is_disabled:
                    badge = "Disabled (0 MB)"
                else:
                    badge = "Sleeping (0 MB)"

            unread_updates = 0
            unread_titles = 0
            latest_unread = 0
            if sid == "suwayomi" and running:
                if self._get_suwayomi_unread is not None:
                    stats = self._get_suwayomi_unread(force=force)
                    unread_updates = stats.get("unread_count", 0)
                    unread_titles = stats.get("unread_titles", 0)
                    latest_unread = stats.get("latest_unread", 0)

            services.append({
                "id": sid,
                "name": cfg["name"],
                "category": cfg.get("category", "General"),
                "running": running,
                "ondemand": False,
                "pid": pids[0] if pids else None,
                "rss_mb": rss_mb,
                "status_badge": badge,
                "port": cfg.get("port"),
                "open_url": cfg.get("open_url"),
                "open_label": cfg.get("open_label"),
                "desc": cfg["desc"],
                "can_toggle": cfg.get("can_toggle", True),
                "memory_complete": service_memory_complete,
                "unread_updates": unread_updates,
                "unread_titles": unread_titles,
                "latest_unread": latest_unread,
            })

        tot_mem = round(total_active_mem, 1) if all_active_complete else None
        return {
            "services": services,
            "active_memory_mb": tot_mem,
            "total_active_mem_mb": tot_mem,
            "memory_complete": all_active_complete,
            "estimated_saved_mem_mb": None,
            "sleeping_count": services_sleeping,
        }

    def toggle_service(
        self,
        sid: str,
        enable: Optional[bool] = None,
        services_config: Optional[Mapping[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Toggle service state via supervisor and return updated report."""
        config = self._resolve_config(services_config)
        cfg = config.get(sid)
        if not cfg:
            return {"status": "error", "message": "Unknown service"}
        if cfg.get("is_ondemand"):
            return self.get_services_report(services_config=config)
        is_currently_disabled = (self.disabled_dir / sid).exists()
        target = is_currently_disabled if enable is None else bool(enable)
        try:
            result = subprocess.run([
                "python3", self.supervisor_script,
                "--service", sid, "--enable", str(target).lower()
            ], capture_output=True, text=True, timeout=25)
            if result.returncode:
                return {"status": "error", "message": "Service control failed; see server recovery logs"}
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "error", "message": "Service control timed out; see server recovery logs"}

        if target:
            for _ in range(15):
                pids = self._get_service_pids(cfg["pattern"])
                if pids:
                    break
                time.sleep(0.1)

        return self.get_services_report(services_config=config)
