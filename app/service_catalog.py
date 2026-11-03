"""Single source of truth for service identities, endpoint facts, and views.

Import purity guarantee:
Importing this module performs no filesystem access, subprocess calls,
network requests, secret reads, or background work.
"""

from typing import Any, Dict, List, Optional


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
    return {
        'ssh': dict(port=8022, cmd=[f'{p}/bin/sshd', '-D']),
        'dashboard': dict(port=8001, path='/healthz', cmd=[f'{p}/bin/python3', '-u', f'{p}/bin/dashboard']),
        'caddy': dict(port=8000, path='/healthz', cmd=[f'{p}/bin/caddy', 'run', '--config', f'{h}/.config/caddy/Caddyfile']),
        'filebrowser': dict(port=8085, path='/drive/', cmd=[f'{p}/bin/filebrowser', '-d', f'{h}/.config/filebrowser/filebrowser.db', '-a', '127.0.0.1', '-p', '8085', '-c', f'{h}/.config/filebrowser/fb_config.json', '--cacheDir', f'{h}/.cache/filebrowser', '--disableImageResolutionCalc', '--imageProcessors', '2']),
        'transfer_gateway': dict(port=8081, path='/healthz', cmd=[f'{h}/bin/mobile-gateway', '-backend', 'http://127.0.0.1:8085']),
        'adguard': dict(port=3000, path='/', cmd=[f'{h}/adguard/AdGuardHome/AdGuardHome', '-c', f'{h}/adguard/data/AdGuardHome.yaml', '-w', f'{h}/adguard/data', '--no-permcheck', '--no-etc-hosts']),
        'syncthing': dict(port=8384, path='/rest/noauth/health', cmd=[f'{p}/bin/syncthing', 'serve', '--config', f'{h}/.local/state/syncthing', '--data', f'{h}/.local/state/syncthing', '--allow-newer-config', '--no-browser']),
        'vaultwarden': dict(port=8082, path='/alive', cmd=[f'{p}/bin/proot-distro', 'login', 'debian', '--', '/usr/bin/env', 'ROCKET_PORT=8082', 'ROCKET_ADDRESS=127.0.0.1', f'DATA_FOLDER={h}/vaultwarden/data', f'WEB_VAULT_FOLDER={h}/vaultwarden/web-vault', f'{h}/vaultwarden/vaultwarden']),
        'terminal': dict(port=7681, path='/terminal/', cmd=[f'{p}/bin/ttyd', '-W', '-i', '127.0.0.1', '-p', '7681', '-b', '/terminal', '-I', f'{h}/.config/ttyd/termux_index.html', f'{h}/bin/termux_session.sh']),
        'agy': dict(port=7682, path='/agy/', cmd=[f'{p}/bin/ttyd', '-W', '-a', '-m', '8', '-i', '127.0.0.1', '-p', '7682', '-b', '/agy', '-I', f'{h}/.config/ttyd/agy_index.html', f'{h}/bin/agy_session.sh']),
        'laptop_terminal': dict(port=7683, path='/laptop/', cmd=[f'{p}/bin/ttyd', '-W', '-a', '-m', '8', '-i', '127.0.0.1', '-p', '7683', '-b', '/laptop', f'{p}/bin/python3', f'{h}/bin/laptop_session.py']),
        'tor': dict(port=9050, protocol='socks5', cmd=[f'{p}/bin/tor', '-f', f'{h}/.config/tor/torrc']),
        'suwayomi': dict(port=4569, path='/suwayomi/', cmd=[f'{h}/bin/start_suwayomi.sh']),
        'suwayomi_filter': dict(port=4566, path='/healthz', cmd=[f'{p}/bin/python3', f'{h}/bin/suwayomi_graphql_filter.py']),
        'battery_guard': dict(cmd=[f'{p}/bin/bash', f'{h}/bin/battery_guard.sh', '--daemon']),
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
            "port": 8085,
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
            "port": 4567,
            "pattern": "Suwayomi-Server",
            "icon": "book-open",
            "desc": "Self-Hosted Manga & Comic Library with Tachiyomi/Mihon Cloud Sync (Port 4567)",
            "open_url": "/suwayomi/",
            "open_label": "Open Reader",
            "can_toggle": True
        },
        "vaultwarden": {
            "id": "vaultwarden",
            "name": "Bitwarden Vault Server",
            "category": "Security & Identity",
            "port": 8082,
            "pattern": "vaultwarden/vaultwarden",
            "icon": "key",
            "desc": "Self-Hosted End-to-End Encrypted Bitwarden Vault (Port 8082)",
            "open_url": "javascript:openVaultHTTPS()",
            "open_label": "Open Vault (HTTPS)",
            "can_toggle": True
        },
        "adguard": {
            "id": "adguard",
            "name": "AdGuard Home DNS",
            "category": "Network & Privacy",
            "port": 3000,
            "pattern": "AdGuardHome",
            "icon": "shield",
            "desc": "Whole-Network Ad-Blocking & Private DNS (Ports 3000, 5353)",
            "open_url": "http://100.75.159.53:3000",
            "open_label": "Open Console",
            "can_toggle": True
        },
        "terminal": {
            "id": "terminal",
            "name": "Admin Linux Shell",
            "category": "System & Control",
            "port": 7681,
            "pattern": "ttyd.*7681",
            "icon": "terminal",
            "desc": "Headless Server Web Console & Root Access (Port 7681)",
            "open_url": "/terminal/",
            "open_label": "Open Shell",
            "can_toggle": True
        },
        "agy": {
            "id": "agy",
            "name": "AGY AI Pair Programmer",
            "category": "Autonomous AI",
            "port": 7682,
            "pattern": "ttyd.*7682",
            "icon": "sparkles",
            "desc": "Autonomous Coding Agent with Persistent Session & Image Paste (Port 7682)",
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
            "port": 8384,
            "pattern": "syncthing",
            "icon": "refresh",
            "desc": "Continuous Bi-directional Real-Time Sync (Sem3 &bull; Port 8384)",
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
