"""Single source of truth for service identities, endpoint facts, and views.

Import purity guarantee:
Importing this module performs no filesystem access, subprocess calls,
network requests, secret reads, or background work.
"""

from typing import Any, Dict, List, Optional

# Shared identity and endpoint facts: port, path, protocol, etc.
# Distinguishes Suwayomi internal backend 4569, filter 4566, and native/dashboard 4567.
SERVICE_ENDPOINTS: Dict[str, Dict[str, Any]] = {
    'ssh': {'port': 8022},
    'dashboard': {'port': 8001, 'path': '/healthz'},
    'caddy': {'port': 8000, 'path': '/healthz'},
    'filebrowser': {'port': 8085, 'path': '/drive/'},
    'transfer_gateway': {'port': 8081, 'path': '/healthz'},
    'adguard': {'port': 3000, 'path': '/'},
    'syncthing': {'port': 8384, 'path': '/rest/noauth/health'},
    'vaultwarden': {'port': 8082, 'path': '/alive'},
    'terminal': {'port': 7681, 'path': '/terminal/'},
    'agy': {'port': 7682, 'path': '/agy/'},
    'laptop_terminal': {'port': 7683, 'path': '/laptop/'},
    'tor': {'port': 9050, 'protocol': 'socks5'},
    'suwayomi': {'port': 4569, 'path': '/suwayomi/', 'client_port': 4567},
    'suwayomi_filter': {'port': 4566, 'path': '/healthz'},
    'battery_guard': {},
    'converter': {'is_ondemand': True},
}


def suwayomi_probe_ports() -> List[int]:
    """Return fallback reachability ports for Suwayomi probing [4569, 4567]."""
    backend_port = SERVICE_ENDPOINTS['suwayomi']['port']
    client_port = SERVICE_ENDPOINTS['suwayomi']['client_port']
    return [backend_port, client_port]


def runtime_services(home: str, prefix: str) -> Dict[str, Dict[str, Any]]:
    """Build supervisor runtime configuration for all managed services.

    Args:
        home: Absolute path string for HOME directory.
        prefix: Absolute path string for PREFIX directory.

    Returns:
        A fresh, mutable dictionary mapping service ID to runtime attributes:
        port (optional int), path (optional str), protocol (optional str),
        cmd (List[str]).
    """
    h, p = str(home), str(prefix)
    fb_port = str(SERVICE_ENDPOINTS['filebrowser']['port'])
    gw_backend_port = fb_port
    vw_port = str(SERVICE_ENDPOINTS['vaultwarden']['port'])

    return {
        'ssh': dict(port=SERVICE_ENDPOINTS['ssh']['port'], cmd=[f'{p}/bin/sshd', '-D']),
        'dashboard': dict(port=SERVICE_ENDPOINTS['dashboard']['port'], path=SERVICE_ENDPOINTS['dashboard']['path'], cmd=[f'{p}/bin/python3', '-u', f'{p}/bin/dashboard']),
        'caddy': dict(port=SERVICE_ENDPOINTS['caddy']['port'], path=SERVICE_ENDPOINTS['caddy']['path'], cmd=[f'{p}/bin/caddy', 'run', '--config', f'{h}/.config/caddy/Caddyfile']),
        'filebrowser': dict(port=SERVICE_ENDPOINTS['filebrowser']['port'], path=SERVICE_ENDPOINTS['filebrowser']['path'], cmd=[f'{p}/bin/filebrowser', '-d', f'{h}/.config/filebrowser/filebrowser.db', '-a', '127.0.0.1', '-p', fb_port, '-c', f'{h}/.config/filebrowser/fb_config.json', '--cacheDir', f'{h}/.cache/filebrowser', '--disableImageResolutionCalc', '--imageProcessors', '2']),
        'transfer_gateway': dict(port=SERVICE_ENDPOINTS['transfer_gateway']['port'], path=SERVICE_ENDPOINTS['transfer_gateway']['path'], cmd=[f'{h}/bin/mobile-gateway', '-backend', f'http://127.0.0.1:{gw_backend_port}']),
        'adguard': dict(port=SERVICE_ENDPOINTS['adguard']['port'], path=SERVICE_ENDPOINTS['adguard']['path'], cmd=[f'{h}/adguard/AdGuardHome/AdGuardHome', '-c', f'{h}/adguard/data/AdGuardHome.yaml', '-w', f'{h}/adguard/data', '--no-permcheck', '--no-etc-hosts']),
        'syncthing': dict(port=SERVICE_ENDPOINTS['syncthing']['port'], path=SERVICE_ENDPOINTS['syncthing']['path'], cmd=[f'{p}/bin/syncthing', 'serve', '--config', f'{h}/.local/state/syncthing', '--data', f'{h}/.local/state/syncthing', '--allow-newer-config', '--no-browser']),
        'vaultwarden': dict(port=SERVICE_ENDPOINTS['vaultwarden']['port'], path=SERVICE_ENDPOINTS['vaultwarden']['path'], cmd=[f'{p}/bin/proot-distro', 'login', 'debian', '--', '/usr/bin/env', f'ROCKET_PORT={vw_port}', 'ROCKET_ADDRESS=127.0.0.1', f'DATA_FOLDER={h}/vaultwarden/data', f'WEB_VAULT_FOLDER={h}/vaultwarden/web-vault', f'{h}/vaultwarden/vaultwarden']),
        'terminal': dict(port=SERVICE_ENDPOINTS['terminal']['port'], path=SERVICE_ENDPOINTS['terminal']['path'], cmd=[f'{p}/bin/ttyd', '-W', '-i', '127.0.0.1', '-p', str(SERVICE_ENDPOINTS['terminal']['port']), '-b', '/terminal', '-I', f'{h}/.config/ttyd/termux_index.html', f'{h}/bin/termux_session.sh']),
        'agy': dict(port=SERVICE_ENDPOINTS['agy']['port'], path=SERVICE_ENDPOINTS['agy']['path'], cmd=[f'{p}/bin/ttyd', '-W', '-a', '-m', '8', '-i', '127.0.0.1', '-p', str(SERVICE_ENDPOINTS['agy']['port']), '-b', '/agy', '-I', f'{h}/.config/ttyd/agy_index.html', f'{h}/bin/agy_session.sh']),
        'laptop_terminal': dict(port=SERVICE_ENDPOINTS['laptop_terminal']['port'], path=SERVICE_ENDPOINTS['laptop_terminal']['path'], cmd=[f'{p}/bin/ttyd', '-W', '-a', '-m', '8', '-i', '127.0.0.1', '-p', str(SERVICE_ENDPOINTS['laptop_terminal']['port']), '-b', '/laptop', f'{p}/bin/python3', f'{h}/bin/laptop_session.py']),
        'tor': dict(port=SERVICE_ENDPOINTS['tor']['port'], protocol=SERVICE_ENDPOINTS['tor']['protocol'], cmd=[f'{p}/bin/tor', '-f', f'{h}/.config/tor/torrc']),
        'suwayomi': dict(port=SERVICE_ENDPOINTS['suwayomi']['port'], path=SERVICE_ENDPOINTS['suwayomi']['path'], cmd=[f'{h}/bin/start_suwayomi.sh']),
        'suwayomi_filter': dict(port=SERVICE_ENDPOINTS['suwayomi_filter']['port'], path=SERVICE_ENDPOINTS['suwayomi_filter']['path'], cmd=[f'{p}/bin/python3', f'{h}/bin/suwayomi_graphql_filter.py']),
        'battery_guard': dict(cmd=[f'{p}/bin/bash', f'{h}/bin/battery_guard.sh', '--daemon']),
        'suwayomi_scheduler': dict(cmd=[f'{p}/bin/python3', f'{h}/bin/suwayomi_daily_update.py', '--foreground']),
    }


def dashboard_services() -> Dict[str, Dict[str, Any]]:
    """Build dashboard display and toggle metadata for user-facing service cards.

    Returns:
        A fresh, mutable dictionary mapping service ID to display configuration.
        Stale/unused start_cmd, stop_cmd, and env dictionaries are omitted.
    """
    return {
        "filebrowser": {
            "id": "filebrowser",
            "name": "Cloud Storage & Media",
            "category": "Storage & Media",
            "port": SERVICE_ENDPOINTS['filebrowser']['port'],
            "pattern": "filebrowser -d",
            "icon": "folder",
            "desc": "Family Cloud Storage, Photos, Music & Google Files-style Media Engine",
            "open_url": "/drive/",
            "open_label": "Master Drive",
            "can_toggle": True
        },
        "suwayomi": {
            "id": "suwayomi",
            "name": "Suwayomi Manga Server",
            "category": "Storage & Media",
            "port": SERVICE_ENDPOINTS['suwayomi']['client_port'],
            "pattern": "Suwayomi-Server",
            "icon": "book-open",
            "desc": f"Self-Hosted Manga & Comic Library with Tachiyomi/Mihon Cloud Sync (Port {SERVICE_ENDPOINTS['suwayomi']['client_port']})",
            "open_url": "/suwayomi/",
            "open_label": "Open Reader",
            "can_toggle": True
        },
        "vaultwarden": {
            "id": "vaultwarden",
            "name": "Bitwarden Vault Server",
            "category": "Security & Identity",
            "port": SERVICE_ENDPOINTS['vaultwarden']['port'],
            "pattern": "vaultwarden/vaultwarden",
            "icon": "key",
            "desc": f"Self-Hosted End-to-End Encrypted Bitwarden Vault (Port {SERVICE_ENDPOINTS['vaultwarden']['port']})",
            "open_url": "javascript:openVaultHTTPS()",
            "open_label": "Open Vault (HTTPS)",
            "can_toggle": True
        },
        "adguard": {
            "id": "adguard",
            "name": "AdGuard Home DNS",
            "category": "Network & Privacy",
            "port": SERVICE_ENDPOINTS['adguard']['port'],
            "pattern": "AdGuardHome",
            "icon": "shield",
            "desc": "Whole-Network Ad-Blocking & Private DNS (Ports 3000, 5353)",
            "open_url": f"http://100.75.159.53:{SERVICE_ENDPOINTS['adguard']['port']}",
            "open_label": "Open Console",
            "can_toggle": True
        },
        "terminal": {
            "id": "terminal",
            "name": "Admin Linux Shell",
            "category": "System & Control",
            "port": SERVICE_ENDPOINTS['terminal']['port'],
            "pattern": "ttyd.*7681",
            "icon": "terminal",
            "desc": f"Headless Server Web Console & Root Access (Port {SERVICE_ENDPOINTS['terminal']['port']})",
            "open_url": "/terminal/",
            "open_label": "Open Shell",
            "can_toggle": True
        },
        "agy": {
            "id": "agy",
            "name": "AGY AI Pair Programmer",
            "category": "Autonomous AI",
            "port": SERVICE_ENDPOINTS['agy']['port'],
            "pattern": "ttyd.*7682",
            "icon": "sparkles",
            "desc": f"Autonomous Coding Agent with Persistent Session & Image Paste (Port {SERVICE_ENDPOINTS['agy']['port']})",
            "open_url": "/agy/",
            "open_label": "Launch AGY",
            "can_toggle": True
        },
        "converter": {
            "id": "converter",
            "name": "Document to PDF Converter",
            "category": "Productivity Tools",
            "port": None,
            "pattern": "libreoffice",
            "is_ondemand": True,
            "icon": "document",
            "desc": "LibreOffice Headless Engine (Word/PPT/Images to PDF &bull; 0 MB Idle)",
            "open_url": "javascript:openConverterModal()",
            "open_label": "Convert File",
            "can_toggle": False
        },
        "syncthing": {
            "id": "syncthing",
            "name": "Syncthing Continuous Sync",
            "category": "File Synchronization",
            "port": SERVICE_ENDPOINTS['syncthing']['port'],
            "pattern": "syncthing",
            "icon": "refresh",
            "desc": f"Continuous Bi-directional Real-Time Sync (Sem3 &bull; Port {SERVICE_ENDPOINTS['syncthing']['port']})",
            "open_url": "javascript:openSyncthingGUI()",
            "open_label": "Open Syncthing GUI",
            "can_toggle": True
        },
        "battery_guard": {
            "id": "battery_guard",
            "name": "Battery & Hardware Protection",
            "category": "Hardware & Power",
            "port": None,
            "pattern": "battery_guard.sh --daemon",
            "icon": "shield",
            "desc": "Battery and temperature monitoring with alerts",
            "open_url": "javascript:alert('Battery Guard monitors readings and sends alerts; it cannot control charging hardware.');",
            "open_label": "Battery Monitor",
            "can_toggle": True
        }
    }
