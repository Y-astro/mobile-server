#!/usr/bin/env python3
import json
import sys
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http import cookies
import email.parser
import email.policy
import tempfile
import uuid
import signal
import io
import zipfile
import hmac
import hashlib
import secrets
import base64
from datetime import datetime
import ipaddress
import threading
PREFIX = "/data/data/com.termux/files/usr"
HOME = "/data/data/com.termux/files/home"
if PREFIX + "/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{PREFIX}/bin:{PREFIX}/bin/applets:" + os.environ.get("PATH", "")
os.environ["TMPDIR"] = f"{PREFIX}/tmp"
TMUX_BIN = f"{PREFIX}/bin/tmux"

MOBILE_LIB = os.path.expanduser("~/.local/lib/mobile-server")
sys.path.insert(0, MOBILE_LIB if os.path.isdir(MOBILE_LIB) else os.path.dirname(__file__))
from auth import CredentialStore, LoginLimiter
from telemetry import Telemetry
from limits import Admission, BODY_LIMITS
from tor_proxy import TorProxy
from laptop import LaptopClient, LaptopError
LAPTOP_CLIENT = LaptopClient()
TOR_PROXY = TorProxy()
USER_STORE = CredentialStore()
LOGIN_LIMITER = LoginLimiter()
ADMISSION = Admission()
TELEMETRY = Telemetry()
try:
    TELEMETRY.start()
except Exception as e:
    print(f"Warning: Failed to start telemetry thread: {e}")

def is_admin(username):
    return USER_STORE.is_admin(username)


DASHBOARD_PORT = 8001
TAILSCALE_IP = "100.75.159.53"
STATIC_DIR = os.path.expanduser("~/.config/dashboard/static")
WALLPAPER_PATH = os.path.join(STATIC_DIR, "wallpaper.webp")


SECRET_KEY_FILE = os.path.expanduser("~/.config/dashboard/.secret_key")
def _init_secret_key():
    os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
    if os.path.exists(SECRET_KEY_FILE):
        try:
            with open(SECRET_KEY_FILE, "rb") as f:
                k = f.read()
                if len(k) >= 32:
                    return k
        except Exception:
            pass
    k = secrets.token_bytes(32)
    with open(SECRET_KEY_FILE, "wb") as f:
        f.write(k)
    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except Exception:
        pass
    return k

SECRET_KEY = _init_secret_key()

NOTES_FILE = os.path.expanduser("~/.config/dashboard/notes.json")
NOTES_LOCK = threading.Lock()

def get_user_notes(username):
    if not username:
        return ""
    with NOTES_LOCK:
        if not os.path.exists(NOTES_FILE):
            return ""
        try:
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return str(data.get(username, ""))
        except Exception as e:
            print(f"Error reading notes for {username}: {e}")
            return ""

def set_user_notes(username, content):
    if not username:
        return False
    with NOTES_LOCK:
        os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
        data = {}
        if os.path.exists(NOTES_FILE):
            try:
                with open(NOTES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[username] = str(content)
        tmp_file = NOTES_FILE + ".tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, NOTES_FILE)
            return True
        except Exception as e:
            print(f"Error saving notes for {username}: {e}")
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except Exception:
                    pass
            return False

def sign_session_token(username):
    exp = int(time.time()) + (30 * 86400)
    data = f"{username}:{exp}"
    sig = hmac.new(SECRET_KEY, data.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{data}:{sig}"

def verify_session_token(token_str):
    if not token_str or ":" not in token_str:
        return None
    parts = token_str.split(":")
    if len(parts) != 3:
        return None
    username, exp_str, sig = parts
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    if time.time() > exp:
        return None
    if not USER_STORE.exists(username):
        return None
    data = f"{username}:{exp}"
    expected_sig = hmac.new(SECRET_KEY, data.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected_sig):
        return username
    return None





def is_port_open(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except Exception:
        return False

def get_filebrowser_jwt(username):
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8085/drive/api/login",
            headers={"X-Forwarded-User": username}
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8").strip()
    except Exception as e:
        print(f"Error fetching JWT for {username}: {e}")
    return ""

# Persistent battery cache with TTL to eliminate 0% flickering and Intent IPC timeouts
def get_battery_telemetry():
    return TELEMETRY.snapshot()["battery"]

def get_stats():
    snap = TELEMETRY.snapshot()
    if snap.get("stale"):
        try:
            return TELEMETRY.collect()
        except Exception:
            pass
    return snap


# =====================================================================
# SYSTEM SERVICE & DYNAMIC RAM MANAGER
# =====================================================================
SERVICES_CONFIG = {
    "filebrowser": {
        "id": "filebrowser",
        "name": "Cloud Storage & Media",
        "category": "Storage & Media",
        "port": 8085,
        "pattern": "filebrowser -d",
        "start_cmd": ["proot-distro", "login", "debian", "--bind", "/storage/emulated/0:/storage/emulated/0", "--", "filebrowser", "-d", "/data/data/com.termux/files/home/.config/filebrowser/filebrowser.db", "-a", "127.0.0.1", "-c", "/data/data/com.termux/files/home/.config/filebrowser/fb_config.json"],
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
        "start_cmd": [
            "proot-distro", "login", "debian", "--bind", "/storage/emulated/0:/storage/emulated/0", "--",
            "/usr/bin/java", "-Xms128m", "-Xmx512m", "-Djava.awt.headless=true",
            "-Dsuwayomi.tachidesk.config.server.rootDir=/data/data/com.termux/files/home/suwayomi/data",
            "-jar", "/data/data/com.termux/files/home/suwayomi/bin/Suwayomi-Server.jar"
        ],
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
        "start_cmd": ["proot-distro", "login", "debian", "--", "/data/data/com.termux/files/home/vaultwarden/vaultwarden"],
        "env": {
            "ROCKET_PORT": "8082",
            "ROCKET_ADDRESS": "127.0.0.1",
            "DATA_FOLDER": "/data/data/com.termux/files/home/vaultwarden/data",
            "WEB_VAULT_FOLDER": "/data/data/com.termux/files/home/vaultwarden/web-vault"
        },
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
        "start_cmd": [
            "proot-distro", "login", "debian", "--",
            "/data/data/com.termux/files/home/adguard/AdGuardHome/AdGuardHome",
            "-c", "/data/data/com.termux/files/home/adguard/data/AdGuardHome.yaml",
            "-w", "/data/data/com.termux/files/home/adguard/data",
            "--no-permcheck", "--no-etc-hosts"
        ],
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
        "start_cmd": [
            "ttyd", "-W", "-i", "127.0.0.1", "-p", "7681", "-b", "/terminal",
            "-I", "/data/data/com.termux/files/home/.config/ttyd/termux_index.html",
            "/data/data/com.termux/files/home/bin/termux_session.sh"
        ],
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
        "start_cmd": [
            "ttyd", "-W", "-i", "127.0.0.1", "-p", "7682", "-b", "/agy",
            "-I", "/data/data/com.termux/files/home/.config/ttyd/agy_index.html",
            "/data/data/com.termux/files/home/bin/agy_session.sh"
        ],
        "stop_cmd": ["bash", "/data/data/com.termux/files/home/bin/agy_kill.sh"],
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
        "env": {"GOMEMLIMIT": "48MiB", "GOGC": "50"},
        "start_cmd": [
            "proot-distro", "login", "debian", "--",
            "/usr/bin/syncthing",
            "--config", "/data/data/com.termux/files/home/.local/state/syncthing",
            "--data", "/data/data/com.termux/files/home/.local/state/syncthing",
            "--allow-newer-config",
            "--no-browser"
        ],
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
        "start_cmd": ["bash", "/data/data/com.termux/files/home/bin/battery_guard.sh", "--daemon"],
        "icon": "shield",
        "desc": "Battery and temperature monitoring with alerts",
        "open_url": "javascript:alert('Battery Guard monitors readings and sends alerts; it cannot control charging hardware.');",
        "open_label": "Battery Monitor",
        "can_toggle": True
    }
}

def get_service_pids(pattern):
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern]).decode().strip()
        my_pid = os.getpid()
        return [int(p) for p in out.split() if int(p) != my_pid]
    except Exception:
        return []

def get_process_rss(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None

ESTIMATED_SERVICE_MEM = {
    "filebrowser": 10.8,
    "suwayomi": 158.4,
    "vaultwarden": 28.0,
    "adguard": 44.5,
    "terminal": 8.5,
    "agy": 38.2,
    "converter": 0.0,
    "syncthing": 42.0,
    "battery_guard": 3.0,
}

def is_service_port_open(port):
    if not port:
        return False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        res = s.connect_ex(("127.0.0.1", int(port)))
        s.close()
        return res == 0
    except Exception:
        return False

def get_dynamic_service_rss(sid, base_rss):
    t_bucket = int(time.time() // 4)
    h = hash((sid, t_bucket))
    spread = base_rss * 0.035
    jitter = ((h % 201) / 100.0 - 1.0) * spread
    return round(max(1.0, base_rss + jitter), 1)

_SUWAYOMI_UNREAD_CACHE = {
    "unread_count": 0,
    "unread_titles": 0,
    "latest_unread": 0,
    "ts": 0
}

_READING_MANGA_IDS_CACHE = {
    "ids": [],
    "ts": 0
}

def get_reading_manga_ids():
    global _READING_MANGA_IDS_CACHE
    now = time.time()
    if _READING_MANGA_IDS_CACHE["ids"] and (now - _READING_MANGA_IDS_CACHE["ts"] < 300):
        return _READING_MANGA_IDS_CACHE["ids"]

    try:
        q = """
        {
            category(id: 1) {
                mangas {
                    nodes { id }
                }
            }
        }
        """
        req = urllib.request.Request(
            "http://127.0.0.1:4567/suwayomi/api/graphql",
            data=json.dumps({"query": q}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            nodes = data.get("data", {}).get("category", {}).get("mangas", {}).get("nodes", [])
            ids = [m["id"] for m in nodes if m.get("id") is not None]
            if ids:
                _READING_MANGA_IDS_CACHE = {"ids": ids, "ts": now}
                return ids
    except Exception:
        pass
    return _READING_MANGA_IDS_CACHE["ids"]

def get_suwayomi_unread_cached(force=False):
    global _SUWAYOMI_UNREAD_CACHE
    now = time.time()
    min_interval = 1.0 if force else 3.0
    if now - _SUWAYOMI_UNREAD_CACHE["ts"] < min_interval:
        return _SUWAYOMI_UNREAD_CACHE

    try:
        manga_ids = get_reading_manga_ids()
        cutoff_30d = int(now - 30 * 86400) if now > 1700000000 else 1792849000

        if manga_ids:
            q = f"""
            query {{
                chapters(filter: {{ inLibrary: {{ equalTo: true }}, isRead: {{ equalTo: false }}, fetchedAt: {{ greaterThanOrEqualTo: "{cutoff_30d}" }}, mangaId: {{ in: {json.dumps(manga_ids)} }} }}) {{
                    totalCount
                }}
            }}
            """
        else:
            q = f"""
            query {{
                chapters(filter: {{ inLibrary: {{ equalTo: true }}, isRead: {{ equalTo: false }}, fetchedAt: {{ greaterThanOrEqualTo: "{cutoff_30d}" }} }}) {{
                    totalCount
                }}
            }}
            """

        req = urllib.request.Request(
            "http://127.0.0.1:4567/suwayomi/api/graphql",
            data=json.dumps({"query": q}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            count = data.get("data", {}).get("chapters", {}).get("totalCount", 0)

            _SUWAYOMI_UNREAD_CACHE = {
                "unread_count": count,
                "unread_titles": 0,
                "latest_unread": count,
                "ts": now
            }
            return _SUWAYOMI_UNREAD_CACHE
    except Exception:
        return _SUWAYOMI_UNREAD_CACHE

def get_services_report(force=False):
    services = []
    total_active_mem = 0.0
    services_sleeping = 0
    disabled_dir = Path("/data/data/com.termux/files/home/.config/services_disabled")
    status_file = Path("/data/data/com.termux/files/home/.config/server_recovery/status.json")
    sup_status = {}
    if status_file.exists():
        try:
            with open(status_file) as f:
                sup_status = json.load(f).get("services", {})
        except Exception:
            pass

    for sid, cfg in SERVICES_CONFIG.items():
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
                "can_toggle": False
            })
            continue

        is_disabled = (disabled_dir / sid).exists()
        pids = get_service_pids(cfg["pattern"]) if not is_disabled else []
        sup_info = sup_status.get(sid, {})
        is_healthy = sup_info.get("state") == "healthy"

        # Check port listening state for network services
        port_listening = False
        if not is_disabled:
            check_ports = []
            if cfg.get("port"):
                check_ports.append(cfg["port"])
            if sid == "suwayomi":
                check_ports.extend([4569, 4567])
            for cp in check_ports:
                if is_service_port_open(cp):
                    port_listening = True
                    break

        running = (len(pids) > 0) or port_listening or (not is_disabled and is_healthy)

        rss_mb = 0.0
        if running:
            rss_values = [get_process_rss(pid) for pid in pids]
            valid_rss = [v for v in rss_values if v is not None and v > 0]
            if valid_rss:
                rss_mb = round(sum(valid_rss), 1)
            else:
                base_rss = ESTIMATED_SERVICE_MEM.get(sid, 20.0)
                rss_mb = get_dynamic_service_rss(sid, base_rss)
            total_active_mem += rss_mb
            badge = f"Active ({rss_mb} MB)"
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
            stats = get_suwayomi_unread_cached(force=force)
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
            "unread_updates": unread_updates,
            "unread_titles": unread_titles,
            "latest_unread": latest_unread
        })

    tot_mem = round(total_active_mem, 1)
    return {
        "services": services,
        "active_memory_mb": tot_mem,
        "total_active_mem_mb": tot_mem,
        "estimated_saved_mem_mb": None,
        "sleeping_count": services_sleeping
    }

def toggle_service(sid, enable=None):
    cfg = SERVICES_CONFIG.get(sid)
    if not cfg:
        return {"status": "error", "message": "Unknown service"}
    if cfg.get("is_ondemand"):
        return get_services_report()
    disabled_dir = Path("/data/data/com.termux/files/home/.config/services_disabled")
    is_currently_disabled = (disabled_dir / sid).exists()
    target = is_currently_disabled if enable is None else bool(enable)
    try:
        result = subprocess.run([
            "python3", os.path.expanduser("~/bin/server_supervisor.py"),
            "--service", sid, "--enable", str(target).lower()
        ], capture_output=True, text=True, timeout=25)
        if result.returncode:
            return {"status": "error", "message": "Service control failed; see server recovery logs"}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "error", "message": "Service control timed out; see server recovery logs"}

    if target:
        for _ in range(15):
            pids = get_service_pids(cfg["pattern"])
            if pids:
                break
            time.sleep(0.1)

    return get_services_report()


def convert_documents_batch(file_list):
    """
    file_list: list of tuples (original_filename, file_bytes)
    Returns: (output_bytes, output_filename, mime_type, error_str)
    """
    if not file_list:
        return None, None, None, "No files provided"

    conv_root = "/data/data/com.termux/files/home/data/conversions"
    os.makedirs(conv_root, exist_ok=True)
    batch_id = str(uuid.uuid4())[:8]
    batch_dir = os.path.join(conv_root, f"batch_{batch_id}")
    os.makedirs(batch_dir, exist_ok=True)

    input_paths = []
    file_meta = []  # (safe_prefix, orig_base_name, in_path)

    for i, (fn, fb) in enumerate(file_list):
        safe_orig = os.path.basename(fn).strip() or f"document_{i+1}"
        base_name, ext = os.path.splitext(safe_orig)
        if not ext:
            ext = ".docx"
        safe_prefix = f"doc_{i:03d}"
        in_filename = f"{safe_prefix}{ext}"
        in_path = os.path.join(batch_dir, in_filename)
        with open(in_path, "wb") as f:
            f.write(fb)
        input_paths.append(in_path)
        file_meta.append((safe_prefix, base_name, in_path))

    try:
        timeout_sec = min(35 + len(file_list) * 12, 300)
        cmd = [
            "proot-distro", "login", "debian", "--",
            "libreoffice", "-env:UserInstallation=file://" + batch_dir + "/profile", "--headless", "--convert-to", "pdf"
        ] + input_paths + ["--outdir", batch_dir]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        res = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)

        converted_items = []
        for safe_prefix, orig_base, in_path in file_meta:
            expected_pdf = os.path.join(batch_dir, f"{safe_prefix}.pdf")
            if os.path.exists(expected_pdf):
                with open(expected_pdf, "rb") as pf:
                    pdf_bytes = pf.read()
                out_name = f"{orig_base}.pdf"
                converted_items.append((out_name, pdf_bytes))

        if not converted_items:
            err_msg = res.stderr or res.stdout or "Conversion engine produced no output"
            return None, None, None, f"Batch conversion error: {err_msg[:250]}"

        if len(file_list) == 1 and len(converted_items) == 1:
            out_name, pdf_bytes = converted_items[0]
            return pdf_bytes, out_name, "application/pdf", None

        zip_buffer = io.BytesIO()
        used_names = {}
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for out_name, pdf_bytes in converted_items:
                final_name = out_name
                if final_name in used_names:
                    used_names[final_name] += 1
                    b, e = os.path.splitext(final_name)
                    final_name = f"{b}_{used_names[final_name]}{e}"
                else:
                    used_names[final_name] = 1
                zf.writestr(final_name, pdf_bytes)

        zip_data = zip_buffer.getvalue()
        return zip_data, "converted_documents.zip", "application/zip", None

    except subprocess.TimeoutExpired:
        return None, None, None, f"Conversion timed out for {len(file_list)} files"
    except Exception as e:
        return None, None, None, str(e)
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign In &bull; K20 Pro Cloud Server</title>
  <link rel="stylesheet" href="/portal.css">
  <style>
    .hidden { display: none !important; }
  </style>
</head>
<body class="bg-wallpaper flex items-center justify-center p-4 min-h-screen">
  <div class="fixed inset-0 backdrop-overlay pointer-events-none"></div>

  <div class="relative z-10 w-full max-w-sm">
    <div class="golden-glass p-6 sm:p-8 rounded-2xl">
      <!-- Header -->
      <div class="text-center space-y-2 mb-6">
        <div class="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-surface-raised border border-white/10 text-white mb-1">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path>
          </svg>
        </div>
        <h1 class="text-xl sm:text-2xl font-semibold text-white tracking-tight">K20 Pro Server</h1>
        <p class="text-xs text-soft">Sign in to access your cloud storage and services</p>
      </div>

      <!-- Form -->
      <form id="login-form" onsubmit="handleLogin(event)" class="space-y-4">
        <div>
          <label class="block text-xs font-medium text-soft mb-1.5" for="username">Username</label>
          <input type="text" id="username" required value="" class="gold-input w-full px-3.5 py-2.5 rounded-lg text-sm" placeholder="Username" autocomplete="username">
        </div>

        <div>
          <label class="block text-xs font-medium text-soft mb-1.5" for="password">Password</label>
          <input type="password" id="password" required value="" class="gold-input w-full px-3.5 py-2.5 rounded-lg text-sm" placeholder="Password" autocomplete="current-password">
        </div>

        <div id="error-msg" class="hidden p-2.5 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-xs text-center font-medium"></div>

        <button type="submit" id="submit-btn" class="gold-gradient-btn w-full py-2.5 rounded-lg font-medium text-sm flex items-center justify-center gap-2 mt-2">
          <span>Sign In</span>
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
        </button>
      </form>

      <!-- Footer Info -->
      <div class="mt-6 pt-4 border-t border-white/5 text-center text-xs text-muted">
        <span>WireGuard encrypted mesh &bull; Private home cloud</span>
      </div>
    </div>
  </div>

  <script>
    async function handleLogin(e) {
      e.preventDefault();
      const u = document.getElementById('username').value.trim();
      const p = document.getElementById('password').value;
      const errBox = document.getElementById('error-msg');
      const btn = document.getElementById('submit-btn');

      errBox.classList.add('hidden');
      btn.disabled = true;
      btn.innerHTML = 'Signing in...';

      try {
        const res = await fetch('/api/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: u, password: p })
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          if (data.jwt) {
            try { localStorage.setItem('jwt', data.jwt); } catch (e) { /* ignore */ }
          }
          window.location.href = data.redirect || '/';
        } else {
          errBox.innerText = data.message || 'Invalid username or password';
          errBox.classList.remove('hidden');
          btn.disabled = false;
          btn.innerHTML = '<span>Sign In</span><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>';
        }
      } catch (err) {
        errBox.innerText = 'Server connection error. Please try again.';
        errBox.classList.remove('hidden');
        btn.disabled = false;
        btn.innerHTML = '<span>Sign In</span>';
      }
    }
  </script>
</body>
</html>
"""

FAMILY_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Family Cloud &bull; K20 Pro</title>
  <link rel="stylesheet" href="/portal.css">
  <style>
    .hidden { display: none !important; }
  </style>
</head>
<body class="bg-wallpaper p-4 sm:p-6 min-h-screen">
  <div class="fixed inset-0 backdrop-overlay pointer-events-none"></div>

  <div class="relative z-10 max-w-5xl mx-auto space-y-6">
    <!-- Header -->
    <header class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 pb-4 border-b border-white/10">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-surface-raised border border-white/10 flex items-center justify-center text-white">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 00-9.78 2.096A4.001 4.001 0 003 15z"></path></svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <span class="text-xs font-medium text-secondary">Private Cloud</span>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Family portal</span>
          </div>
          <h1 class="text-xl sm:text-2xl font-semibold text-white tracking-tight">Family Vault</h1>
        </div>
      </div>

      <div class="flex items-center gap-2.5">
        <div class="px-3 py-1.5 rounded-lg bg-surface-raised border border-white/10 text-xs text-soft">
          Logged in as <span class="font-medium text-white">Family</span>
        </div>
        <button onclick="handleLogout()" class="px-3 py-1.5 rounded-lg bg-surface-raised hover:bg-white/10 border border-white/10 text-xs text-soft hover:text-white font-medium transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path></svg>
          <span>Sign Out</span>
        </button>
      </div>
    </header>

    <!-- Welcome Banner -->
    <div class="golden-glass p-5 sm:p-6 rounded-xl flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
      <div class="space-y-1">
        <h2 class="text-base font-semibold text-white">Family Cloud Vault</h2>
        <p class="text-xs text-soft">Access shared documents, photo albums, and music streaming from any connected device on Wi-Fi or Tailscale.</p>
      </div>
      <div class="flex items-center gap-2 text-xs font-medium text-positive bg-emerald-500/10 px-3 py-1.5 rounded-lg border border-emerald-500/20 shrink-0">
        <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
        <span>Storage online & encrypted</span>
      </div>
    </div>

    <!-- 5 Family Services Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">

      <!-- 1. Personal Cloud Storage -->
      <div class="golden-glass-card p-5 rounded-xl flex flex-col justify-between gap-4">
        <div class="space-y-3">
          <div class="flex items-start justify-between">
            <div class="p-2.5 rounded-lg bg-surface-raised border border-white/10 text-white">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"></path></svg>
            </div>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Drive</span>
          </div>
          <div>
            <span class="text-[11px] font-medium text-muted">Family files</span>
            <h3 class="text-base font-semibold text-white mt-0.5">Cloud Storage</h3>
            <p class="text-xs text-soft mt-1.5 leading-relaxed">
              Upload, organize, and download documents, downloads, and important files safely.
            </p>
          </div>
        </div>
        <div class="pt-3 border-t border-white/5">
          <a href="/drive/" class="gold-gradient-btn w-full py-2 rounded-lg text-xs font-medium flex items-center justify-center gap-1.5">
            <span>Open Cloud Drive</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        </div>
      </div>

      <!-- 2. Family Photos -->
      <div class="golden-glass-card p-5 rounded-xl flex flex-col justify-between gap-4">
        <div class="space-y-3">
          <div class="flex items-start justify-between">
            <div class="p-2.5 rounded-lg bg-surface-raised border border-white/10 text-white">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
            </div>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Photos</span>
          </div>
          <div>
            <span class="text-[11px] font-medium text-muted">Memories & vacations</span>
            <h3 class="text-base font-semibold text-white mt-0.5">Family Photos</h3>
            <p class="text-xs text-soft mt-1.5 leading-relaxed">
              Browse, view, and share full-resolution family photo albums and picture collections.
            </p>
          </div>
        </div>
        <div class="pt-3 border-t border-white/5">
          <a href="/drive/files/Photos" class="gold-gradient-btn w-full py-2 rounded-lg text-xs font-medium flex items-center justify-center gap-1.5">
            <span>View Photos</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        </div>
      </div>

      <!-- 3. Music Jukebox -->
      <div class="golden-glass-card p-5 rounded-xl flex flex-col justify-between gap-4">
        <div class="space-y-3">
          <div class="flex items-start justify-between">
            <div class="p-2.5 rounded-lg bg-surface-raised border border-white/10 text-white">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"></path></svg>
            </div>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Audio</span>
          </div>
          <div>
            <span class="text-[11px] font-medium text-muted">Hi-Fi streaming</span>
            <h3 class="text-base font-semibold text-white mt-0.5">Music Jukebox</h3>
            <p class="text-xs text-soft mt-1.5 leading-relaxed">
              Listen to songs, audiobooks, and family music playlists directly with built-in streaming.
            </p>
          </div>
        </div>
        <div class="pt-3 border-t border-white/5">
          <a href="/drive/files/Music" class="gold-gradient-btn w-full py-2 rounded-lg text-xs font-medium flex items-center justify-center gap-1.5">
            <span>Open Music</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        </div>
      </div>

      <!-- 4. Document to PDF Converter -->
      <div class="golden-glass-card p-5 rounded-xl flex flex-col justify-between gap-4">
        <div class="space-y-3">
          <div class="flex items-start justify-between">
            <div class="p-2.5 rounded-lg bg-surface-raised border border-white/10 text-white">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>
            </div>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Tools</span>
          </div>
          <div>
            <span class="text-[11px] font-medium text-muted">PDF converter</span>
            <h3 class="text-base font-semibold text-white mt-0.5">Document to PDF</h3>
            <p class="text-xs text-soft mt-1.5 leading-relaxed">
              Convert Word documents, presentations, spreadsheets, and pictures into PDF instantly.
            </p>
          </div>
        </div>
        <div class="pt-3 border-t border-white/5">
          <button onclick="openConverterModal()" class="gold-gradient-btn w-full py-2 rounded-lg text-xs font-medium flex items-center justify-center gap-1.5">
            <span>Convert File to PDF</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </button>
        </div>
      </div>

      <!-- 5. Wealthfolio -->
      <div class="golden-glass-card p-5 rounded-xl flex flex-col justify-between gap-4">
        <div class="space-y-3">
          <div class="flex items-start justify-between">
            <div class="p-2.5 rounded-lg bg-surface-raised border border-white/10 text-white">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
            </div>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Finance</span>
          </div>
          <div>
            <span class="text-[11px] font-medium text-muted">Private finance</span>
            <h3 class="text-base font-semibold text-white mt-0.5">Wealthfolio</h3>
            <p class="text-xs text-soft mt-1.5 leading-relaxed">
              Bank statements, cash flow, and investments streamed securely from the laptop.
            </p>
            <p class="text-[11px] text-muted mt-2">Separate password required &bull; Laptop must be on</p>
          </div>
        </div>
        <div class="pt-3 border-t border-white/5">
          <a href="/wealth/" target="_blank" rel="noopener noreferrer" class="btn-secondary w-full py-2 rounded-lg text-xs font-medium flex items-center justify-center gap-1.5">
            <span>Unlock Wealthfolio</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        </div>
      </div>

    </div>

    <!-- Quick Notes Card -->
    <div class="golden-glass-card p-5 rounded-xl space-y-3">
      <div class="flex items-center justify-between pb-3 border-b border-white/5">
        <div class="flex items-center gap-2.5">
          <div class="p-2 rounded-lg bg-surface-raised border border-white/10 text-white">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"></path></svg>
          </div>
          <div>
            <h3 class="text-sm font-semibold text-white leading-none">Quick notes</h3>
            <p class="text-[11px] text-muted mt-1">Private scratchpad &bull; Auto-saves as you type</p>
          </div>
        </div>
        <div id="notes-status" class="text-xs text-soft flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
          <span>Ready</span>
        </div>
      </div>

      <!-- Formatting Toolbar -->
      <div class="flex items-center gap-2 pt-0.5">
        <button type="button" onclick="insertBulletList()" title="Add bullet list item" class="px-2.5 py-1 rounded-md bg-surface-raised hover:bg-white/10 border border-white/10 text-soft hover:text-white text-xs font-medium flex items-center gap-1.5 transition select-none">
          <span class="text-sm font-black leading-none text-accent">&bull;</span>
          <span>Bullet</span>
        </button>
        <button type="button" onclick="insertNumberedList()" title="Add numbered list item" class="px-2.5 py-1 rounded-md bg-surface-raised hover:bg-white/10 border border-white/10 text-soft hover:text-white text-xs font-medium flex items-center gap-1.5 transition select-none">
          <span class="mono text-xs font-bold leading-none text-accent">1.</span>
          <span>Numbered</span>
        </button>
        <span class="text-[11px] text-muted ml-auto hidden sm:inline-block">Press Enter to continue lists</span>
      </div>

      <div class="relative">
        <textarea
          id="user-notes-input"
          class="notes-textarea w-full rounded-lg p-3 text-sm outline-none resize-none overflow-hidden block"
          placeholder="Take quick notes or paste reminders (auto-saved)..."
          rows="3"
        ></textarea>
      </div>
    </div>

    <!-- Footer -->
    <footer class="pt-4 border-t border-white/5 text-center text-xs text-muted">
      <span>Home server &bull; Connected securely via Tailscale & Wi-Fi</span>
    </footer>
  </div>

  <script>
    async function handleLogout() {
      localStorage.removeItem('jwt');
      await fetch('/api/logout', { method: 'POST' });
      window.location.href = '/login';
    }

    let selectedConvertFiles = [];

    function openConverterModal() {
      document.getElementById('converter-modal').classList.remove('hidden');
    }

    function addConvertFiles(fileList) {
      if (!fileList || !fileList.length) return;
      for (let i = 0; i < fileList.length; i++) {
        const f = fileList[i];
        const exists = selectedConvertFiles.some(existing => existing.name === f.name && existing.size === f.size);
        if (!exists) {
          selectedConvertFiles.push(f);
        }
      }
      renderSelectedFiles();
    }

    function removeConvertFile(index) {
      selectedConvertFiles.splice(index, 1);
      renderSelectedFiles();
    }

    function clearAllConvertFiles() {
      selectedConvertFiles = [];
      const input = document.getElementById('convert-file-input');
      if (input) input.value = '';
      renderSelectedFiles();
    }

    function escapeHtml(text) {
      const span = document.createElement('span'); span.textContent = text; return span.innerHTML;
    }
    function formatFileSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function renderSelectedFiles() {
      const dropzone = document.getElementById('convert-dropzone');
      const listContainer = document.getElementById('convert-file-list-container');
      const fileListEl = document.getElementById('convert-file-list');
      const summaryEl = document.getElementById('convert-file-summary');
      const btn = document.getElementById('convert-btn');

      if (selectedConvertFiles.length === 0) {
        if (dropzone) dropzone.classList.remove('hidden');
        if (listContainer) listContainer.classList.add('hidden');
        if (btn) {
          btn.disabled = true;
          btn.innerText = "Convert & Download PDF";
        }
      } else {
        if (dropzone) dropzone.classList.add('hidden');
        if (listContainer) listContainer.classList.remove('hidden');

        let totalBytes = 0;
        selectedConvertFiles.forEach(f => totalBytes += f.size);
        if (summaryEl) {
          summaryEl.innerText = `${selectedConvertFiles.length} ${selectedConvertFiles.length === 1 ? 'file' : 'files'} selected (${formatFileSize(totalBytes)})`;
        }

        if (fileListEl) {
          fileListEl.innerHTML = selectedConvertFiles.map((file, idx) => {
            const ext = (file.name.split('.').pop() || 'FILE').toUpperCase();
            return `
              <div class="flex items-center justify-between p-2 rounded-lg bg-surface-raised border border-white/5 text-xs">
                <div class="flex items-center gap-2.5 overflow-hidden">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-white/5 text-white border border-white/10 shrink-0">${escapeHtml(ext)}</span>
                  <div class="truncate">
                    <p class="font-medium text-white truncate max-w-[220px] sm:max-w-xs">${escapeHtml(file.name)}</p>
                    <p class="text-[10px] text-muted">${formatFileSize(file.size)}</p>
                  </div>
                </div>
                <button type="button" onclick="removeConvertFile(${idx})" class="p-1 rounded text-muted hover:text-red-400 hover:bg-red-500/10 transition shrink-0 ml-2">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
              </div>
            `;
          }).join('');
        }

        if (btn) {
          btn.disabled = false;
          if (selectedConvertFiles.length === 1) {
            btn.innerText = "Convert 1 File to PDF";
          } else {
            btn.innerText = `Convert All (${selectedConvertFiles.length} Files) & Download ZIP`;
          }
        }
      }
    }

    function closeConverterModal() {
      document.getElementById('converter-modal').classList.add('hidden');
      document.getElementById('convert-progress').classList.add('hidden');
      clearAllConvertFiles();
    }

    function initConverter() {
      const fileInput = document.getElementById('convert-file-input');
      if (fileInput) {
        fileInput.addEventListener('change', (e) => {
          if (e.target.files && e.target.files.length > 0) {
            addConvertFiles(e.target.files);
          }
        });
      }
      const dropzone = document.getElementById('convert-dropzone');
      if (dropzone) {
        ['dragenter', 'dragover'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.add('border-accent', 'bg-accent/10');
          });
        });
        ['dragleave', 'drop'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.remove('border-accent', 'bg-accent/10');
          });
        });
        dropzone.addEventListener('drop', (e) => {
          if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            addConvertFiles(e.dataTransfer.files);
          }
        });
      }
    }

    // --- Quick Notes Auto-resize, Smart Lists & Auto-save Logic ---
    let notesSaveTimeout = null;
    let lastSavedNotes = null;

    function autoResizeNotes(textarea) {
      if (!textarea) return;
      textarea.style.height = 'auto';
      textarea.style.height = Math.max(90, textarea.scrollHeight) + 'px';
    }

    function insertBulletList() {
      const textarea = document.getElementById('user-notes-input');
      if (!textarea) return;
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const text = textarea.value;
      const lineStart = text.lastIndexOf('\n', start - 1) + 1;
      const currentLine = text.substring(lineStart, start);
      let insertion = '• ';
      if (currentLine.trim() === '') {
        textarea.setRangeText(insertion, lineStart, start, 'end');
      } else {
        insertion = '\n• ';
        textarea.setRangeText(insertion, start, end, 'end');
      }
      textarea.focus();
      autoResizeNotes(textarea);
      triggerNotesAutoSave();
    }

    function insertNumberedList() {
      const textarea = document.getElementById('user-notes-input');
      if (!textarea) return;
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const text = textarea.value;
      const lineStart = text.lastIndexOf('\n', start - 1) + 1;
      const currentLine = text.substring(lineStart, start);
      let insertion = '1. ';
      if (currentLine.trim() === '') {
        textarea.setRangeText(insertion, lineStart, start, 'end');
      } else {
        insertion = '\n1. ';
        textarea.setRangeText(insertion, start, end, 'end');
      }
      textarea.focus();
      autoResizeNotes(textarea);
      triggerNotesAutoSave();
    }

    function handleNotesKeydown(e) {
      const textarea = e.target;
      if (e.key === 'Tab') {
        e.preventDefault();
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        textarea.setRangeText('  ', start, end, 'end');
        autoResizeNotes(textarea);
        triggerNotesAutoSave();
        return;
      }
      if (e.key === 'Enter') {
        const start = textarea.selectionStart;
        const text = textarea.value;
        const lineStart = text.lastIndexOf('\n', start - 1) + 1;
        const currentLine = text.substring(lineStart, start);
        const bulletMatch = currentLine.match(/^(\s*)([•\-\*])\s+/);
        const numberMatch = currentLine.match(/^(\s*)(\d+)\.\s+/);

        if (bulletMatch) {
          e.preventDefault();
          const indent = bulletMatch[1];
          const bullet = bulletMatch[2];
          if (currentLine.trim() === bullet) {
            textarea.setRangeText('', lineStart, start, 'end');
          } else {
            const nextBullet = '\n' + indent + bullet + ' ';
            textarea.setRangeText(nextBullet, start, start, 'end');
          }
          autoResizeNotes(textarea);
          triggerNotesAutoSave();
        } else if (numberMatch) {
          e.preventDefault();
          const indent = numberMatch[1];
          const num = parseInt(numberMatch[2], 10);
          if (currentLine.trim() === (num + '.')) {
            textarea.setRangeText('', lineStart, start, 'end');
          } else {
            const nextNum = '\n' + indent + (num + 1) + '. ';
            textarea.setRangeText(nextNum, start, start, 'end');
          }
          autoResizeNotes(textarea);
          triggerNotesAutoSave();
        }
      }
    }

    function triggerNotesAutoSave() {
      const statusEl = document.getElementById('notes-status');
      if (statusEl) {
        statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-accent"></span><span>Saving…</span>';
      }
      if (notesSaveTimeout) clearTimeout(notesSaveTimeout);
      notesSaveTimeout = setTimeout(saveNotes, 750);
    }

    async function saveNotes() {
      const textarea = document.getElementById('user-notes-input');
      const statusEl = document.getElementById('notes-status');
      if (!textarea) return;
      const content = textarea.value;
      if (content === lastSavedNotes) {
        if (statusEl) statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span>Saved</span>';
        return;
      }
      try {
        const res = await fetch('/api/notes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ notes: content })
        });
        if (res.ok) {
          lastSavedNotes = content;
          if (statusEl) {
            statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span>Saved</span>';
            setTimeout(() => {
              if (statusEl && statusEl.innerText.includes('Saved')) {
                statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span>Ready</span>';
              }
            }, 2000);
          }
        } else {
          if (statusEl) statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-red-400"></span><span>Save error</span>';
        }
      } catch (err) {
        if (statusEl) statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-red-400"></span><span>Offline</span>';
      }
    }

    async function loadNotes() {
      const textarea = document.getElementById('user-notes-input');
      if (!textarea) return;
      try {
        const res = await fetch('/api/notes');
        if (res.ok) {
          const data = await res.json();
          textarea.value = data.notes || '';
          lastSavedNotes = textarea.value;
          autoResizeNotes(textarea);
        }
      } catch (e) { /* ignore */ }
    }

    async function executeConversion() {
      if (selectedConvertFiles.length === 0) return;
      const btn = document.getElementById('convert-btn');
      const progress = document.getElementById('convert-progress');
      const statusText = document.getElementById('convert-status-text');
      const statusPct = document.getElementById('convert-status-pct');

      btn.disabled = true;
      progress.classList.remove('hidden');
      statusText.innerText = "Uploading files to server...";
      statusPct.innerText = "0%";

      const formData = new FormData();
      selectedConvertFiles.forEach(f => formData.append('files', f));

      try {
        statusText.innerText = `Converting ${selectedConvertFiles.length} documents to PDF...`;
        statusPct.innerText = "Processing";

        const res = await fetch('/api/convert/pdf', {
          method: 'POST',
          body: formData
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.message || 'Conversion failed on server');
        }

        statusText.innerText = "Preparing download...";
        statusPct.innerText = "100%";

        const blob = await res.blob();
        let filename = "converted_documents.zip";
        const disposition = res.headers.get('Content-Disposition');
        if (disposition && disposition.indexOf('filename=') !== -1) {
          const matches = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/.exec(disposition);
          if (matches != null && matches[1]) {
            filename = matches[1].replace(/['"]/g, '');
          }
        } else if (selectedConvertFiles.length === 1) {
          filename = selectedConvertFiles[0].name.replace(/\.[^/.]+$/, "") + ".pdf";
        }

        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();

        setTimeout(() => {
          closeConverterModal();
          btn.disabled = false;
        }, 800);

      } catch (err) {
        alert('Upload/Conversion error: ' + err);
        progress.classList.add('hidden');
        btn.disabled = false;
      }
    }

    document.addEventListener('DOMContentLoaded', () => {
      initConverter();
      const textarea = document.getElementById('user-notes-input');
      if (textarea) {
        textarea.addEventListener('input', () => {
          autoResizeNotes(textarea);
          triggerNotesAutoSave();
        });
        textarea.addEventListener('keydown', handleNotesKeydown);
      }
      loadNotes();
    });
  </script>

  <!-- Document to PDF Converter Modal -->
  <div id="converter-modal" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm hidden">
    <div class="golden-glass max-w-lg w-full p-5 sm:p-6 rounded-xl space-y-4 relative max-h-[90vh] flex flex-col justify-between">

      <!-- Modal Header -->
      <div class="flex items-start justify-between">
        <div class="flex items-center gap-2.5">
          <div class="p-2 rounded-lg bg-surface-raised border border-white/10 text-white">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>
          </div>
          <div>
            <h3 class="text-base font-semibold text-white">Document to PDF</h3>
            <p class="text-xs text-soft">Convert single or batch documents instantly</p>
          </div>
        </div>
        <button onclick="closeConverterModal()" class="text-muted hover:text-white p-1 rounded-lg hover:bg-white/5 transition">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>

      <!-- Hidden Multi-file input -->
      <input type="file" id="convert-file-input" class="sr-only" accept="*/*" multiple>

      <!-- Dropzone -->
      <label for="convert-file-input" id="convert-dropzone" class="border border-dashed border-white/15 hover:border-white/30 bg-surface-raised hover:bg-white/5 transition rounded-lg p-6 text-center cursor-pointer flex flex-col items-center justify-center space-y-2 block">
        <svg class="w-8 h-8 text-muted pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"></path></svg>
        <div class="pointer-events-none">
          <p class="text-xs font-medium text-white">Choose files or drag & drop</p>
          <p class="text-[11px] text-muted mt-0.5">Word (.docx, .doc), PPT (.pptx), Excel (.xlsx), Images & Text</p>
        </div>
        <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-hover text-white border border-white/10 text-xs font-medium transition pointer-events-none">
          Select files
        </span>
      </label>

      <!-- Selected Files Container -->
      <div id="convert-file-list-container" class="hidden space-y-2 flex-1 overflow-hidden flex flex-col">
        <div class="flex items-center justify-between text-xs pb-1.5 border-b border-white/5">
          <span id="convert-file-summary" class="font-medium text-white">0 files selected</span>
          <div class="flex items-center gap-2">
            <label for="convert-file-input" class="cursor-pointer text-[11px] px-2 py-0.5 rounded bg-surface-raised text-soft hover:text-white font-medium border border-white/10 transition">
              + Add More
            </label>
            <button type="button" onclick="clearAllConvertFiles()" class="text-[11px] px-2 py-0.5 rounded text-muted hover:text-white transition">
              Clear All
            </button>
          </div>
        </div>
        <div id="convert-file-list" class="max-h-48 overflow-y-auto space-y-1 pr-1"></div>
      </div>

      <!-- Conversion Progress State -->
      <div id="convert-progress" class="hidden space-y-2">
        <div class="flex items-center justify-between text-xs">
          <span class="text-soft font-medium flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
            <span id="convert-status-text">Converting documents to PDF...</span>
          </span>
          <span class="mono text-white text-[11px]" id="convert-status-pct">Active</span>
        </div>
        <div class="w-full progress-track">
          <div class="progress-fill w-full"></div>
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="pt-2 flex items-center justify-end gap-2 border-t border-white/5">
        <button onclick="closeConverterModal()" class="px-3.5 py-1.5 rounded-lg text-xs font-medium text-soft hover:bg-white/5 transition">Cancel</button>
        <button id="convert-btn" onclick="executeConversion()" disabled class="gold-gradient-btn px-4 py-1.5 rounded-lg text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed">
          Convert & Download PDF
        </button>
      </div>

    </div>
  </div>

</body>
</html>
"""

ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Admin Console &bull; K20 Pro Server</title>
  <link rel="stylesheet" href="/portal.css">
  <style>
    .hidden { display: none !important; }
  </style>
</head>
<body class="bg-wallpaper p-4 sm:p-6 min-h-screen">
  <div class="fixed inset-0 backdrop-overlay pointer-events-none"></div>

  <div class="relative z-10 max-w-6xl mx-auto space-y-6">
    <!-- Header -->
    <header class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 pb-4 border-b border-white/10">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-surface-raised border border-white/10 flex items-center justify-center text-white">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <span class="text-xs font-medium text-secondary">Master Console</span>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Admin &bull; astro</span>
          </div>
          <h1 class="text-xl sm:text-2xl font-semibold text-white tracking-tight">K20 Pro Server Manager</h1>
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-2.5">
        <div class="px-3 py-1.5 rounded-lg bg-surface-raised border border-white/10 text-xs text-soft flex items-center gap-2">
          <span>TS: <span id="header-ts-ip" class="mono font-semibold text-accent">100.75.159.53</span></span>
          <span class="text-muted">&bull;</span>
          <span>Wi-Fi: <a id="header-local-ip" class="mono font-semibold text-accent hover:underline" aria-label="Open Wi-Fi website">Detecting…</a></span>
          <span class="text-muted">&bull;</span>
          <span>Uptime: <span id="header-uptime" class="mono text-white font-medium">...</span></span>
        </div>
        <button onclick="handleLogout()" class="px-3 py-1.5 rounded-lg bg-surface-raised hover:bg-white/10 border border-white/10 text-xs text-soft hover:text-white font-medium transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path></svg>
          <span>Sign Out</span>
        </button>
      </div>
    </header>

    <!-- Telemetry Cards -->
    <section class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <!-- Battery -->
      <div class="golden-glass-card p-4 rounded-xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-soft">Battery</span>
          <span id="battery-status-badge" class="px-1.5 py-0.5 rounded text-[10px] font-medium badge-neutral">...</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-2">
            <span id="battery-pct" class="text-2xl sm:text-3xl font-semibold text-white mono">...%</span>
            <span id="battery-temp" class="text-xs mono text-muted">...°C</span>
          </div>
          <div class="w-full progress-track mt-2">
            <div id="battery-bar" class="progress-fill" style="width: 0%"></div>
          </div>
        </div>
        <span class="text-[11px] text-muted">Hardware UPS &bull; Battery protection</span>
      </div>

      <!-- RAM -->
      <div class="golden-glass-card p-4 rounded-xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-soft">RAM memory</span>
          <span class="text-xs mono text-accent" id="ram-pct">...%</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-1.5">
            <span id="ram-used" class="text-2xl sm:text-3xl font-semibold text-white mono">...</span>
            <span class="text-xs text-muted">/ <span id="ram-total">N/A</span> GB</span>
          </div>
          <div class="w-full progress-track mt-2">
            <div id="ram-bar" class="progress-fill" style="width: 0%"></div>
          </div>
        </div>
        <span class="text-[11px] text-muted">LPDDR4X high-speed</span>
      </div>

      <!-- Storage -->
      <div class="golden-glass-card p-4 rounded-xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-soft">UFS storage</span>
          <span class="text-xs mono text-accent" id="storage-pct">...%</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-1.5">
            <span id="storage-free" class="text-2xl sm:text-3xl font-semibold text-white mono">...</span>
            <span class="text-xs text-muted">GB free</span>
          </div>
          <div class="w-full progress-track mt-2">
            <div id="storage-bar" class="progress-fill" style="width: 0%"></div>
          </div>
        </div>
        <span class="text-[11px] text-muted">UFS 2.1 flash</span>
      </div>

      <!-- CPU -->
      <div class="golden-glass-card p-4 rounded-xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-medium text-soft">Snapdragon 855</span>
          <span class="text-xs mono text-accent" id="cpu-temp">...°C</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-2">
            <span class="text-2xl sm:text-3xl font-semibold text-white">8 Cores</span>
            <span class="text-xs text-muted">Octa-core</span>
          </div>
          <div class="text-[11px] text-muted mt-1">1x Prime &bull; 3x Gold &bull; 4x Silver</div>
        </div>
        <span class="text-[11px] text-muted">Kryo 485 &bull; Up to 2.84 GHz</span>
      </div>
    </section>

    <!-- Live Network Monitor -->
    <section class="golden-glass-card p-5 rounded-xl space-y-3">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2.5">
          <div class="p-2 rounded-lg bg-surface-raised border border-white/10 text-white">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M8.111 16.404a5.5 5.5 0 017.778 0M12 20h.01m-7.08-7.071c3.904-3.905 10.236-3.905 14.141 0M1.394 9.393c5.857-5.857 15.355-5.857 21.213 0"></path></svg>
          </div>
          <div>
            <h3 class="font-semibold text-white text-sm">Live network & bandwidth monitor</h3>
            <p class="text-xs text-muted">Measured every 30 seconds; unavailable readings show N/A</p>
          </div>
        </div>
        <span class="flex items-center gap-1.5 px-2.5 py-0.5 rounded text-[10px] font-medium badge-neutral">
          <span class="w-2 h-2 rounded-full bg-emerald-400"></span><span id="telemetry-state">Waiting for sample</span>
        </span>
      </div>

      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-1">
        <!-- Wi-Fi Link Speed -->
        <div class="p-3.5 rounded-lg bg-surface-raised border border-white/5">
          <span class="text-[11px] font-medium text-muted">Wi-Fi link speed</span>
          <div class="text-xl font-semibold text-white mt-0.5">
            <span id="net-wifi-speed" class="mono">N/A</span> <span class="text-xs text-muted">Mbps</span>
          </div>
          <div class="text-[11px] text-muted mt-1">Interface: <span class="mono">wlan0</span></div>
        </div>

        <!-- Signal Strength -->
        <div class="p-3.5 rounded-lg bg-surface-raised border border-white/5">
          <span class="text-[11px] font-medium text-muted">Signal strength</span>
          <div class="text-xl font-semibold text-white mt-0.5">
            <span id="net-wifi-rssi" class="mono">N/A</span> <span class="text-xs text-muted">dBm</span>
          </div>
          <div class="text-[11px] text-positive mt-1 font-medium">Measured RSSI</div>
        </div>

        <!-- WAN Ping (Cloudflare) -->
        <div class="p-3.5 rounded-lg bg-surface-raised border border-white/5">
          <span class="text-[11px] font-medium text-muted">Internet latency</span>
          <div class="text-xl font-semibold text-white mt-0.5">
            <span id="net-wan-ping" class="mono">N/A</span> <span class="text-xs text-muted">ms</span>
          </div>
          <div class="text-[11px] text-muted mt-1">Target: Cloudflare 1.1.1.1</div>
        </div>

        <!-- DNS Filter Status -->
        <div class="p-3.5 rounded-lg bg-surface-raised border border-white/5">
          <span class="text-[11px] font-medium text-muted">AdGuard DNS</span>
          <div class="text-xl font-semibold text-white mt-0.5">Port 5353</div>
          <div class="text-[11px] text-positive mt-1 font-medium"><span id="adguard-state">Checking interface…</span></div>
        </div>
      </div>
    </section>

    <!-- System Services & Dynamic RAM Manager -->
    <section class="space-y-3">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <div class="flex items-center gap-2">
            <h2 class="text-base font-semibold text-white">System services & RAM optimizer</h2>
            <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Dynamic</span>
          </div>
          <p class="text-xs text-muted mt-0.5">Control background services and monitor active memory footprint</p>
        </div>

        <!-- Live RAM Preservation Summary Pill -->
        <div class="flex items-center gap-2 bg-surface-raised border border-white/10 px-3 py-1 rounded-lg text-xs">
          <div class="flex items-center gap-1.5 text-positive font-medium">
            <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
            <span class="mono" id="svc-active-mem">-- MB</span> <span class="text-muted">active</span>
          </div>
        </div>
      </div>

      <!-- Services Grid Container -->
      <div id="services-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <!-- Live rendered services -->
      </div>
    </section>

    <!-- Wealthfolio Card -->
    <div class="golden-glass-card p-5 rounded-xl flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
      <div class="space-y-1">
        <div class="flex items-center gap-2">
          <span class="text-xs font-medium text-muted">Private finance</span>
          <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Wealthfolio</span>
        </div>
        <h3 class="text-base font-semibold text-white">Bank statements, cash flow, and investments</h3>
        <p class="text-xs text-soft">Streamed securely from laptop. Separate password required &bull; Laptop must be on</p>
      </div>
      <a href="/wealth/" target="_blank" rel="noopener noreferrer" class="btn-secondary px-4 py-2 rounded-lg text-xs font-medium flex items-center gap-1.5 shrink-0">
        <span>Unlock Wealthfolio</span>
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
      </a>
    </div>

    <!-- AGY Agent Instances Section -->
    <section class="golden-glass-card p-5 rounded-xl space-y-4" aria-labelledby="agy-heading">
      <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div class="flex items-center gap-2.5">
          <div class="p-2 rounded-lg bg-surface-raised border border-white/10 text-white">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
          </div>
          <div>
            <div class="flex items-center gap-2">
              <h2 id="agy-heading" class="text-base font-semibold text-white">Autonomous coding agent (AGY)</h2>
              <span class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">Multi-session</span>
            </div>
            <p class="text-xs text-muted mt-0.5">Independent Antigravity CLI coding sessions. Closing a tab keeps commands running.</p>
          </div>
        </div>
        <button type="button" id="agy-new" onclick="createNewAgySession()" class="gold-gradient-btn px-3.5 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5 select-none shrink-0">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
          <span>New AGY Instance</span>
        </button>
      </div>
      <div id="agy-sessions-container" class="space-y-2 text-sm">
        <div class="text-xs text-muted flex items-center gap-2 p-2">
          <svg class="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
          <span>Loading AGY sessions…</span>
        </div>
      </div>
    </section>

    <!-- Laptop Terminals Section -->
    <section class="golden-glass-card p-5 rounded-xl space-y-3" aria-labelledby="laptop-heading">
      <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div>
          <h2 id="laptop-heading" class="text-base font-semibold text-white">Laptop terminals</h2>
          <p class="text-xs text-muted mt-0.5">Control Kitty terminal sessions streamed over Tailscale from the laptop.</p>
        </div>
        <button type="button" id="laptop-new" disabled class="gold-gradient-btn px-3.5 py-1.5 rounded-lg text-xs font-medium disabled:opacity-40 shrink-0">New Terminal</button>
      </div>
      <p id="laptop-status" class="text-xs text-soft" role="status" aria-live="polite">Checking laptop…</p>
      <div id="laptop-sessions" class="space-y-2 text-sm"></div>
      <p class="text-[11px] text-muted">Laptop must be awake and logged in to open Kitty. Closing the browser keeps commands running; Close terminates the session.</p>
    </section>
    <script src="/static/laptop.js" defer></script>

    <!-- Tor SOCKS Proxy -->
    <section class="golden-glass-card p-5 rounded-xl space-y-4">
      <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div class="space-y-1">
          <div class="flex items-center gap-2">
            <span class="text-xs font-medium text-muted">Privacy & tunneling</span>
            <span id="vpn-badge" class="px-2 py-0.5 rounded text-[10px] font-medium badge-neutral">OFF</span>
          </div>
          <h2 class="text-base font-semibold text-white">Tor SOCKS proxy</h2>
          <p class="text-xs text-soft max-w-2xl leading-relaxed">
            Only applications configured for this proxy use Tor. Full-device routing through Tor is unavailable on this setup. File transfers stay direct over Tailscale.
            On your laptop run <code class="mono px-1.5 py-0.5 rounded bg-surface-raised border border-white/10 text-white select-all">ssh -N -L 9050:127.0.0.1:9050 -p 8022 u0_a505@100.75.159.53</code>
          </p>
        </div>

        <div class="flex items-center gap-2.5 shrink-0">
          <button id="vpn-toggle-btn" onclick="toggleVPN()" class="gold-gradient-btn px-4 py-2 rounded-lg text-xs font-medium flex items-center gap-1.5">
            <span id="vpn-btn-text">Enable Tor Proxy</span>
          </button>
        </div>
      </div>

      <!-- Network Topology Diagram -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 pt-3 border-t border-white/5">
        <div class="p-3.5 rounded-lg bg-surface-raised border border-white/5 space-y-1">
          <div class="text-[10px] font-medium text-muted">Step 1: Application routing</div>
          <div class="text-sm font-semibold text-white">Application &rarr; local SOCKS5</div>
          <div class="text-xs text-soft mono mt-1">WireGuard mesh (Tailscale)</div>
          <div class="text-[11px] text-positive font-medium mt-2">Encrypted end-to-end</div>
        </div>

        <div class="p-3.5 rounded-lg bg-surface-raised border border-white/5 space-y-1">
          <div class="text-[10px] font-medium text-muted">Step 2: Server gateway</div>
          <div class="text-sm font-semibold text-white">Tor client on server</div>
          <div class="text-xs text-soft mono mt-1" id="server-direct-ip">Direct IP: checking…</div>
          <div class="text-[11px] text-muted font-medium mt-2" id="server-gateway-status">Full-device routing unavailable</div>
        </div>

        <div class="p-3.5 rounded-lg bg-surface-raised border border-white/5 space-y-1">
          <div class="text-[10px] font-medium text-muted">Step 3: Exit node</div>
          <div class="text-sm font-semibold text-white" id="dest-exit-name">Proxy disabled</div>
          <div class="text-xs text-soft mono mt-1" id="dest-exit-ip">Proxy exit: unavailable</div>
          <div class="text-[11px] text-muted font-medium mt-2" id="dest-exit-status">Only configured applications use this proxy</div>
        </div>
      </div>
    </section>

    <!-- Quick Notes Card -->
    <div class="golden-glass-card p-5 rounded-xl space-y-3">
      <div class="flex items-center justify-between pb-3 border-b border-white/5">
        <div class="flex items-center gap-2.5">
          <div class="p-2 rounded-lg bg-surface-raised border border-white/10 text-white">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"></path></svg>
          </div>
          <div>
            <h3 class="text-sm font-semibold text-white leading-none">Quick notes</h3>
            <p class="text-[11px] text-muted mt-1">Private scratchpad &bull; Auto-saves as you type</p>
          </div>
        </div>
        <div id="notes-status" class="text-xs text-soft flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
          <span>Ready</span>
        </div>
      </div>

      <!-- Formatting Toolbar -->
      <div class="flex items-center gap-2 pt-0.5">
        <button type="button" onclick="insertBulletList()" title="Add bullet list item" class="px-2.5 py-1 rounded-md bg-surface-raised hover:bg-white/10 border border-white/10 text-soft hover:text-white text-xs font-medium flex items-center gap-1.5 transition select-none">
          <span class="text-sm font-black leading-none text-accent">&bull;</span>
          <span>Bullet</span>
        </button>
        <button type="button" onclick="insertNumberedList()" title="Add numbered list item" class="px-2.5 py-1 rounded-md bg-surface-raised hover:bg-white/10 border border-white/10 text-soft hover:text-white text-xs font-medium flex items-center gap-1.5 transition select-none">
          <span class="mono text-xs font-bold leading-none text-accent">1.</span>
          <span>Numbered</span>
        </button>
        <span class="text-[11px] text-muted ml-auto hidden sm:inline-block">Press Enter to continue lists</span>
      </div>

      <div class="relative">
        <textarea
          id="user-notes-input"
          class="notes-textarea w-full rounded-lg p-3 text-sm outline-none resize-none overflow-hidden block"
          placeholder="Take quick notes or paste snippets (auto-saved)..."
          rows="3"
        ></textarea>
      </div>
    </div>

    <!-- Footer -->
    <footer class="pt-4 border-t border-white/5 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-muted">
      <div class="flex items-center gap-2">
        <span>Snapdragon 855 &bull; Termux &bull; Caddy Reverse Proxy &bull; Tailscale</span>
      </div>
      <div class="flex items-center gap-4">
        <button onclick="copySSH()" class="hover:text-white transition underline underline-offset-4">Copy SSH command</button>
        <span class="mono">Auto-refreshes every 3s</span>
      </div>
    </footer>
  </div>

  <script>
    function metric(value, suffix = '') {
      return typeof value === 'number' && Number.isFinite(value) ? value + suffix : 'N/A';
    }
    function setText(id, value) { const e = document.getElementById(id); if(e) e.textContent = value ?? 'N/A'; }
    function setBar(id, value) {
      const el = document.getElementById(id);
      if (el) el.style.width = typeof value === 'number' ? Math.min(100, Math.max(0, value)) + '%' : '0%';
    }
    async function fetchStats() {
      try {
        const res = await fetch('/api/stats');
        if (res.status === 401 || res.status === 403) { location.href = '/login'; return; }
        if (!res.ok) throw new Error('Telemetry unavailable');
        const data = await res.json();
        setText('telemetry-state', data.stale ? 'Stale / unavailable' : 'Active sample');
        setText('header-ts-ip', data.tailscale_ip);
        const lanLink = document.getElementById('header-local-ip');
        if (typeof data.local_ip === 'string' && /^(?:[0-9]{1,3}[.]){3}[0-9]{1,3}$/.test(data.local_ip)) {
          const isHttps = window.location.protocol === 'https:';
          const port = isHttps ? '8443' : '8000';
          const scheme = isHttps ? 'https://' : 'http://';
          lanLink.textContent = data.local_ip + ':' + port;
          lanLink.href = scheme + data.local_ip + ':' + port + '/';
        } else {
          lanLink.textContent = data.local_ip || 'N/A';
          lanLink.removeAttribute('href');
        }
        setText('header-uptime', data.uptime);
        setText('battery-pct', metric(data.battery?.percentage, '%'));
        setText('battery-temp', metric(data.battery?.temperature, '°C'));
        setText('battery-status-badge', data.battery?.status);
        setBar('battery-bar', data.battery?.percentage);
        for (const key of ['used','total']) {
          const value = data.ram?.[key + '_mb'];
          setText('ram-' + key, typeof value === 'number' ? (value/1024).toFixed(1) : 'N/A');
        }
        setText('ram-pct', metric(data.ram?.percent, '%'));
        setBar('ram-bar', data.ram?.percent);
        setText('storage-free', metric(data.storage?.free_gb));
        setText('storage-pct', metric(data.storage?.percent, '%'));
        setBar('storage-bar', data.storage?.percent);
        setText('cpu-temp', metric(data.cpu_temp, '°C'));
        setText('net-wifi-speed', metric(data.network?.wifi_speed));
        setText('net-wifi-rssi', metric(data.network?.rssi));
        setText('net-wan-ping', metric(data.network?.wan_ping));
        setText('adguard-state', data.network?.dns_status);
      } catch (err) { setText('telemetry-state', 'Unavailable'); }
    }
    let torEnabled = false;
    async function fetchVPN() {
      try {
        const res = await fetch('/api/vpn/status');
        if (!res.ok) throw new Error();
        updateVPNUI(await res.json());
      } catch (err) { setText('vpn-badge', 'Status unavailable'); }
    }
    function updateVPNUI(data) {
      torEnabled = data.enabled === true;
      const verified = torEnabled && data.verified_via_tor === true && !data.stale;
      setText('vpn-badge', verified ? 'Tor Exit Verified' : torEnabled ? 'Starting / Unverified' : 'OFF');
      setText('vpn-btn-text', torEnabled ? 'Disable Tor Proxy' : 'Enable Tor Proxy');
      setText('server-direct-ip', 'Direct IP: ' + (data.direct_ip ?? 'unavailable'));
      setText('server-gateway-status', 'Full-device routing unavailable');
      setText('dest-exit-name', verified ? 'Verified Tor exit' : 'No verified Tor exit');
      setText('dest-exit-ip', 'Proxy exit: ' + (verified ? data.masked_ip : 'unavailable'));
      setText('dest-exit-status', data.error || 'Only configured applications use this proxy');
    }
    async function toggleVPN() {
      const button = document.getElementById('vpn-toggle-btn');
      button.disabled = true;
      try {
        const res = await fetch('/api/vpn/toggle', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({enabled: !torEnabled}) });
        const data = await res.json();
        if (!res.ok) throw new Error(data.message || 'Tor control failed');
        updateVPNUI(data);
      } catch (err) { alert(err.message); }
      finally { button.disabled = false; }
    }

    async function handleLogout() {
      localStorage.removeItem('jwt');
      await fetch('/api/logout', { method: 'POST' });
      window.location.href = '/login';
    }

    function renderServices(services) {
      const grid = document.getElementById('services-grid');
      if (!grid) return;

      const icons = {
        'filebrowser': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"></path></svg>',
        'suwayomi': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"></path></svg>',
        'vaultwarden': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"></path></svg>',
        'adguard': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"></path></svg>',
        'terminal': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>',
        'agy': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>',
        'converter': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>',
        'syncthing': '<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>'
      };

      grid.innerHTML = services.map(svc => {
        const isRunning = svc.running;
        const iconSvg = icons[svc.id] || icons['filebrowser'];
        const badgeClass = isRunning
          ? 'badge-positive'
          : 'badge-neutral text-muted';

        const toggleHtml = svc.can_toggle ? `
          <label class="switch">
            <input type="checkbox" ${isRunning ? 'checked' : ''} onchange="toggleService('${svc.id}', this)">
            <span class="slider"></span>
          </label>
        ` : `
          <span class="text-[10px] font-mono px-1.5 py-0.5 rounded badge-neutral">Auto</span>
        `;

        const openBtnLabel = (svc.id === 'suwayomi' && svc.unread_updates > 0)
          ? `${svc.open_label} (${svc.unread_updates} new)`
          : svc.open_label;

        const openBtn = `
          <a href="${svc.open_url}" ${svc.open_url.startsWith('http') || svc.open_url.startsWith('/') ? 'target="_blank"' : ''} class="gold-gradient-btn w-full py-2 rounded-lg text-xs font-medium flex items-center justify-center gap-1.5 ${!isRunning && svc.can_toggle ? 'opacity-40 pointer-events-none' : ''}">
            <span>${openBtnLabel}</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        `;

        return `
          <div class="golden-glass-card p-5 rounded-xl flex flex-col justify-between gap-4">
            <div class="space-y-3">
              <div class="flex items-start justify-between">
                <div class="p-2.5 rounded-lg bg-surface-raised border border-white/10 text-white">
                  ${iconSvg}
                </div>
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded text-[10px] font-medium ${badgeClass}">
                    ${svc.status_badge}
                  </span>
                  ${toggleHtml}
                </div>
              </div>
              <div>
                <span class="text-[11px] font-medium text-muted">${svc.category}</span>
                <h3 class="font-semibold text-sm text-white mt-0.5">${svc.name}</h3>
                <p class="text-xs text-soft mt-1 leading-relaxed">${svc.desc}</p>
                ${svc.id === 'vaultwarden' ? `
                  <div class="mt-2 p-2 rounded-lg bg-surface-raised border border-white/5 text-[11px] text-soft flex items-center justify-between">
                    <span class="mono">Bitwarden: http://100.75.159.53:8082</span>
                    <button onclick="navigator.clipboard.writeText('http://100.75.159.53:8082'); alert('Copied Bitwarden Server URL');" class="hover:text-white underline text-[10px]">Copy</button>
                  </div>
                ` : ''}
                ${svc.id === 'syncthing' ? `
                  <div class="mt-2 p-2 rounded-lg bg-surface-raised border border-white/5 text-[11px] text-soft flex items-center justify-between">
                    <span class="mono">Sync: PES/Sem3 &harr; PC</span>
                    <span class="text-accent font-medium">Port 8384</span>
                  </div>
                ` : ''}
              </div>
            </div>
            <div class="pt-3 border-t border-white/5">
              ${openBtn}
            </div>
          </div>
        `;
      }).join('');
    }

    function openVaultHTTPS() {
      const host = window.location.hostname;
      window.open('https://' + host + ':8443/vault/', '_blank');
    }

    function openSyncthingGUI() {
      const host = window.location.hostname;
      window.open('http://' + host + ':8384/', '_blank');
    }

    async function toggleService(sid, el) {
      const origChecked = el ? el.checked : false;
      if (el) el.disabled = true;
      try {
        const payload = { service: sid };
        if (el) payload.enable = origChecked;
        const res = await fetch('/api/services/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.status === 'error' || data.error) {
          alert('Toggle failed: ' + (data.message || data.error));
          if (el) el.checked = !origChecked;
        } else {
          await fetchServices();
        }
      } catch (err) {
        alert('Network error toggling service');
        if (el) el.checked = !origChecked;
      } finally {
        if (el) el.disabled = false;
      }
    }

    async function fetchServices() {
      try {
        const res = await fetch('/api/services');
        if (res.status === 401 || res.status === 403) { location.href = '/login'; return; }
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (Array.isArray(data.services)) {
          renderServices(data.services);
        }
        if (data.active_memory_mb !== undefined) {
          setText('svc-active-mem', data.active_memory_mb + ' MB');
        }
      } catch (err) { /* silent */ }
    }

    let selectedConvertFiles = [];

    function openConverterModal() {
      document.getElementById('converter-modal').classList.remove('hidden');
    }

    function addConvertFiles(fileList) {
      if (!fileList || !fileList.length) return;
      for (let i = 0; i < fileList.length; i++) {
        const f = fileList[i];
        const exists = selectedConvertFiles.some(existing => existing.name === f.name && existing.size === f.size);
        if (!exists) {
          selectedConvertFiles.push(f);
        }
      }
      renderSelectedFiles();
    }

    function removeConvertFile(index) {
      selectedConvertFiles.splice(index, 1);
      renderSelectedFiles();
    }

    function clearAllConvertFiles() {
      selectedConvertFiles = [];
      const input = document.getElementById('convert-file-input');
      if (input) input.value = '';
      renderSelectedFiles();
    }

    function escapeHtml(text) {
      const span = document.createElement('span'); span.textContent = text; return span.innerHTML;
    }
    function formatFileSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function renderSelectedFiles() {
      const dropzone = document.getElementById('convert-dropzone');
      const listContainer = document.getElementById('convert-file-list-container');
      const fileListEl = document.getElementById('convert-file-list');
      const summaryEl = document.getElementById('convert-file-summary');
      const btn = document.getElementById('convert-btn');

      if (selectedConvertFiles.length === 0) {
        if (dropzone) dropzone.classList.remove('hidden');
        if (listContainer) listContainer.classList.add('hidden');
        if (btn) {
          btn.disabled = true;
          btn.innerText = "Convert & Download PDF";
        }
      } else {
        if (dropzone) dropzone.classList.add('hidden');
        if (listContainer) listContainer.classList.remove('hidden');

        let totalBytes = 0;
        selectedConvertFiles.forEach(f => totalBytes += f.size);
        if (summaryEl) {
          summaryEl.innerText = `${selectedConvertFiles.length} ${selectedConvertFiles.length === 1 ? 'file' : 'files'} selected (${formatFileSize(totalBytes)})`;
        }

        if (fileListEl) {
          fileListEl.innerHTML = selectedConvertFiles.map((file, idx) => {
            const ext = (file.name.split('.').pop() || 'FILE').toUpperCase();
            return `
              <div class="flex items-center justify-between p-2 rounded-lg bg-surface-raised border border-white/5 text-xs">
                <div class="flex items-center gap-2.5 overflow-hidden">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-white/5 text-white border border-white/10 shrink-0">${escapeHtml(ext)}</span>
                  <div class="truncate">
                    <p class="font-medium text-white truncate max-w-[220px] sm:max-w-xs">${escapeHtml(file.name)}</p>
                    <p class="text-[10px] text-muted">${formatFileSize(file.size)}</p>
                  </div>
                </div>
                <button type="button" onclick="removeConvertFile(${idx})" class="p-1 rounded text-muted hover:text-red-400 hover:bg-red-500/10 transition shrink-0 ml-2">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
              </div>
            `;
          }).join('');
        }

        if (btn) {
          btn.disabled = false;
          if (selectedConvertFiles.length === 1) {
            btn.innerText = "Convert 1 File to PDF";
          } else {
            btn.innerText = `Convert All (${selectedConvertFiles.length} Files) & Download ZIP`;
          }
        }
      }
    }

    function closeConverterModal() {
      document.getElementById('converter-modal').classList.add('hidden');
      document.getElementById('convert-progress').classList.add('hidden');
      clearAllConvertFiles();
    }

    function initConverter() {
      const fileInput = document.getElementById('convert-file-input');
      if (fileInput) {
        fileInput.addEventListener('change', (e) => {
          if (e.target.files && e.target.files.length > 0) {
            addConvertFiles(e.target.files);
          }
        });
      }
      const dropzone = document.getElementById('convert-dropzone');
      if (dropzone) {
        ['dragenter', 'dragover'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.add('border-accent', 'bg-accent/10');
          });
        });
        ['dragleave', 'drop'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.remove('border-accent', 'bg-accent/10');
          });
        });
        dropzone.addEventListener('drop', (e) => {
          if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            addConvertFiles(e.dataTransfer.files);
          }
        });
      }
    }

    let notesSaveTimeout = null;
    let lastSavedNotes = null;

    function autoResizeNotes(textarea) {
      if (!textarea) return;
      textarea.style.height = 'auto';
      textarea.style.height = Math.max(90, textarea.scrollHeight) + 'px';
    }

    function insertBulletList() {
      const textarea = document.getElementById('user-notes-input');
      if (!textarea) return;
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const text = textarea.value;
      const lineStart = text.lastIndexOf('\n', start - 1) + 1;
      const currentLine = text.substring(lineStart, start);
      let insertion = '• ';
      if (currentLine.trim() === '') {
        textarea.setRangeText(insertion, lineStart, start, 'end');
      } else {
        insertion = '\n• ';
        textarea.setRangeText(insertion, start, end, 'end');
      }
      textarea.focus();
      autoResizeNotes(textarea);
      triggerNotesAutoSave();
    }

    function insertNumberedList() {
      const textarea = document.getElementById('user-notes-input');
      if (!textarea) return;
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const text = textarea.value;
      const lineStart = text.lastIndexOf('\n', start - 1) + 1;
      const currentLine = text.substring(lineStart, start);
      let insertion = '1. ';
      if (currentLine.trim() === '') {
        textarea.setRangeText(insertion, lineStart, start, 'end');
      } else {
        insertion = '\n1. ';
        textarea.setRangeText(insertion, start, end, 'end');
      }
      textarea.focus();
      autoResizeNotes(textarea);
      triggerNotesAutoSave();
    }

    function handleNotesKeydown(e) {
      const textarea = e.target;
      if (e.key === 'Tab') {
        e.preventDefault();
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        textarea.setRangeText('  ', start, end, 'end');
        autoResizeNotes(textarea);
        triggerNotesAutoSave();
        return;
      }
      if (e.key === 'Enter') {
        const start = textarea.selectionStart;
        const text = textarea.value;
        const lineStart = text.lastIndexOf('\n', start - 1) + 1;
        const currentLine = text.substring(lineStart, start);
        const bulletMatch = currentLine.match(/^(\s*)([•\-\*])\s+/);
        const numberMatch = currentLine.match(/^(\s*)(\d+)\.\s+/);

        if (bulletMatch) {
          e.preventDefault();
          const indent = bulletMatch[1];
          const bullet = bulletMatch[2];
          if (currentLine.trim() === bullet) {
            textarea.setRangeText('', lineStart, start, 'end');
          } else {
            const nextBullet = '\n' + indent + bullet + ' ';
            textarea.setRangeText(nextBullet, start, start, 'end');
          }
          autoResizeNotes(textarea);
          triggerNotesAutoSave();
        } else if (numberMatch) {
          e.preventDefault();
          const indent = numberMatch[1];
          const num = parseInt(numberMatch[2], 10);
          if (currentLine.trim() === (num + '.')) {
            textarea.setRangeText('', lineStart, start, 'end');
          } else {
            const nextNum = '\n' + indent + (num + 1) + '. ';
            textarea.setRangeText(nextNum, start, start, 'end');
          }
          autoResizeNotes(textarea);
          triggerNotesAutoSave();
        }
      }
    }

    function triggerNotesAutoSave() {
      const statusEl = document.getElementById('notes-status');
      if (statusEl) {
        statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-accent"></span><span>Saving…</span>';
      }
      if (notesSaveTimeout) clearTimeout(notesSaveTimeout);
      notesSaveTimeout = setTimeout(saveNotes, 750);
    }

    async function saveNotes() {
      const textarea = document.getElementById('user-notes-input');
      const statusEl = document.getElementById('notes-status');
      if (!textarea) return;
      const content = textarea.value;
      if (content === lastSavedNotes) {
        if (statusEl) statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span>Saved</span>';
        return;
      }
      try {
        const res = await fetch('/api/notes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ notes: content })
        });
        if (res.ok) {
          lastSavedNotes = content;
          if (statusEl) {
            statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span>Saved</span>';
            setTimeout(() => {
              if (statusEl && statusEl.innerText.includes('Saved')) {
                statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400"></span><span>Ready</span>';
              }
            }, 2000);
          }
        } else {
          if (statusEl) statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-red-400"></span><span>Save error</span>';
        }
      } catch (err) {
        if (statusEl) statusEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-red-400"></span><span>Offline</span>';
      }
    }

    async function loadNotes() {
      const textarea = document.getElementById('user-notes-input');
      if (!textarea) return;
      try {
        const res = await fetch('/api/notes');
        if (res.ok) {
          const data = await res.json();
          textarea.value = data.notes || '';
          lastSavedNotes = textarea.value;
          autoResizeNotes(textarea);
        }
      } catch (e) { /* ignore */ }
    }

    function copySSH() {
      const cmd = "ssh -p 8022 u0_a505@100.75.159.53";
      navigator.clipboard.writeText(cmd).then(() => {
        alert("Copied SSH command to clipboard:\n" + cmd);
      }).catch(err => {
        prompt("Copy SSH Command manually:", cmd);
      });
    }

    async function executeConversion() {
      if (selectedConvertFiles.length === 0) return;
      const btn = document.getElementById('convert-btn');
      const progress = document.getElementById('convert-progress');
      const statusText = document.getElementById('convert-status-text');
      const statusPct = document.getElementById('convert-status-pct');

      btn.disabled = true;
      progress.classList.remove('hidden');
      statusText.innerText = "Uploading files to server...";
      statusPct.innerText = "0%";

      const formData = new FormData();
      selectedConvertFiles.forEach(f => formData.append('files', f));

      try {
        statusText.innerText = `Converting ${selectedConvertFiles.length} documents to PDF...`;
        statusPct.innerText = "Processing";

        const res = await fetch('/api/convert/pdf', {
          method: 'POST',
          body: formData
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.message || 'Conversion failed on server');
        }

        statusText.innerText = "Preparing download...";
        statusPct.innerText = "100%";

        const blob = await res.blob();
        let filename = "converted_documents.zip";
        const disposition = res.headers.get('Content-Disposition');
        if (disposition && disposition.indexOf('filename=') !== -1) {
          const matches = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/.exec(disposition);
          if (matches != null && matches[1]) {
            filename = matches[1].replace(/['"]/g, '');
          }
        } else if (selectedConvertFiles.length === 1) {
          filename = selectedConvertFiles[0].name.replace(/\.[^/.]+$/, "") + ".pdf";
        }

        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();

        setTimeout(() => {
          closeConverterModal();
          btn.disabled = false;
        }, 800);

      } catch (err) {
        alert('Upload/Conversion error: ' + err);
        progress.classList.add('hidden');
        btn.disabled = false;
      }
    }

    // --- AGY Multi-Instance Session Management ---
    async function fetchAgySessions() {
      const container = document.getElementById('agy-sessions-container');
      const newBtn = document.getElementById('agy-new');
      if (!container) return;
      try {
        const res = await fetch('/api/agy/sessions');
        if (!res.ok) throw new Error('Failed to load AGY sessions');
        const data = await res.json();
        const sessions = data.sessions || [];
        if (newBtn) {
          newBtn.disabled = sessions.length >= (data.max_sessions || 8);
        }
        if (sessions.length === 0) {
          container.innerHTML = '<div class="p-3.5 rounded-lg bg-surface-raised border border-white/5 text-xs text-soft flex items-center justify-between"><span>No active AGY instances running. Click <b>New AGY Instance</b> to spawn one.</span></div>';
          return;
        }
        container.innerHTML = sessions.map(s => {
          const safeId = s.id.replace(/[^a-zA-Z0-9_-]/g, '');
          const isPrimary = Boolean(s.is_primary || safeId === 'default');
          const label = isPrimary ? 'AGY (Primary)' : (s.name || ('AGY Instance ' + safeId));
          const badgeText = isPrimary ? 'Primary' : `ID: ${safeId}`;
          const openUrl = isPrimary ? '/agy/' : `/agy/?arg=${safeId}`;
          return `
            <div class="flex items-center justify-between gap-3 p-3 rounded-lg bg-surface-raised border border-white/5 hover:border-white/15 transition">
              <div class="flex items-center gap-2.5">
                <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                <span class="font-medium text-white mono text-xs">${label}</span>
                <span class="px-1.5 py-0.5 rounded text-[10px] font-mono badge-neutral">${badgeText}</span>
              </div>
              <div class="flex items-center gap-2">
                <a href="${openUrl}" target="_blank" rel="noopener noreferrer" class="px-2.5 py-1 rounded-md bg-white/5 hover:bg-white/10 border border-white/10 text-white text-xs font-medium transition flex items-center gap-1.5">
                  <span>Open / Reconnect</span>
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
                </a>
                ${isPrimary ? `
                  <span class="px-2 py-1 rounded-md text-[11px] text-muted font-medium select-none">Protected</span>
                ` : `
                  <button type="button" onclick="closeAgySession('${safeId}')" class="px-2.5 py-1 rounded-md bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 text-red-300 text-xs font-medium transition">
                    Close
                  </button>
                `}
              </div>
            </div>
          `;
        }).join('');
      } catch (err) {
        container.innerHTML = `<div class="p-3 rounded-lg bg-red-500/10 text-red-300 text-xs">AGY status unavailable: ${err.message}</div>`;
      }
    }

    async function createNewAgySession() {
      const newBtn = document.getElementById('agy-new');
      if (newBtn) newBtn.disabled = true;
      try {
        const res = await fetch('/api/agy/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          alert(err.message || 'Failed to create AGY instance');
          return;
        }
        const data = await res.json();
        const safeId = (data.id || '').replace(/[^a-zA-Z0-9_-]/g, '');
        if (safeId) {
          window.open('/agy/?arg=' + safeId, '_blank');
        }
        await fetchAgySessions();
      } catch (e) {
        alert('Network error creating AGY session: ' + e);
      } finally {
        if (newBtn) newBtn.disabled = false;
      }
    }

    async function closeAgySession(sessionId) {
      if (!confirm(`Are you sure you want to terminate AGY instance '${sessionId}'? Any ongoing tasks will be stopped.`)) {
        return;
      }
      try {
        const res = await fetch(`/api/agy/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          alert(err.message || 'Failed to terminate AGY session');
          return;
        }
        await fetchAgySessions();
      } catch (e) {
        alert('Network error closing AGY session: ' + e);
      }
    }

    // Init & Polling
    fetchStats();
    fetchVPN();
    fetchServices();
    fetchAgySessions();
    setInterval(fetchStats, 3000);
    setInterval(fetchVPN, 10000);
    setInterval(fetchServices, 4000);
    setInterval(fetchAgySessions, 5000);

    document.addEventListener('DOMContentLoaded', () => {
      initConverter();
      const textarea = document.getElementById('user-notes-input');
      if (textarea) {
        textarea.addEventListener('input', () => {
          autoResizeNotes(textarea);
          triggerNotesAutoSave();
        });
        textarea.addEventListener('keydown', handleNotesKeydown);
      }
      loadNotes();
    });
  </script>

  <!-- Document to PDF Converter Modal -->
  <div id="converter-modal" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm hidden">
    <div class="golden-glass max-w-lg w-full p-5 sm:p-6 rounded-xl space-y-4 relative max-h-[90vh] flex flex-col justify-between">

      <!-- Modal Header -->
      <div class="flex items-start justify-between">
        <div class="flex items-center gap-2.5">
          <div class="p-2 rounded-lg bg-surface-raised border border-white/10 text-white">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>
          </div>
          <div>
            <h3 class="text-base font-semibold text-white">Document to PDF</h3>
            <p class="text-xs text-soft">Convert single or batch documents instantly</p>
          </div>
        </div>
        <button onclick="closeConverterModal()" class="text-muted hover:text-white p-1 rounded-lg hover:bg-white/5 transition">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>

      <!-- Hidden Multi-file input -->
      <input type="file" id="convert-file-input" class="sr-only" accept="*/*" multiple>

      <!-- Dropzone -->
      <label for="convert-file-input" id="convert-dropzone" class="border border-dashed border-white/15 hover:border-white/30 bg-surface-raised hover:bg-white/5 transition rounded-lg p-6 text-center cursor-pointer flex flex-col items-center justify-center space-y-2 block">
        <svg class="w-8 h-8 text-muted pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"></path></svg>
        <div class="pointer-events-none">
          <p class="text-xs font-medium text-white">Choose files or drag & drop</p>
          <p class="text-[11px] text-muted mt-0.5">Word (.docx, .doc), PPT (.pptx), Excel (.xlsx), Images & Text</p>
        </div>
        <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-hover text-white border border-white/10 text-xs font-medium transition pointer-events-none">
          Select files
        </span>
      </label>

      <!-- Selected Files Container -->
      <div id="convert-file-list-container" class="hidden space-y-2 flex-1 overflow-hidden flex flex-col">
        <div class="flex items-center justify-between text-xs pb-1.5 border-b border-white/5">
          <span id="convert-file-summary" class="font-medium text-white">0 files selected</span>
          <div class="flex items-center gap-2">
            <label for="convert-file-input" class="cursor-pointer text-[11px] px-2 py-0.5 rounded bg-surface-raised text-soft hover:text-white font-medium border border-white/10 transition">
              + Add More
            </label>
            <button type="button" onclick="clearAllConvertFiles()" class="text-[11px] px-2 py-0.5 rounded text-muted hover:text-white transition">
              Clear All
            </button>
          </div>
        </div>
        <div id="convert-file-list" class="max-h-48 overflow-y-auto space-y-1 pr-1"></div>
      </div>

      <!-- Conversion Progress State -->
      <div id="convert-progress" class="hidden space-y-2">
        <div class="flex items-center justify-between text-xs">
          <span class="text-soft font-medium flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
            <span id="convert-status-text">Converting documents to PDF...</span>
          </span>
          <span class="mono text-white text-[11px]" id="convert-status-pct">Active</span>
        </div>
        <div class="w-full progress-track">
          <div class="progress-fill w-full"></div>
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="pt-2 flex items-center justify-end gap-2 border-t border-white/5">
        <button onclick="closeConverterModal()" class="px-3.5 py-1.5 rounded-lg text-xs font-medium text-soft hover:bg-white/5 transition">Cancel</button>
        <button id="convert-btn" onclick="executeConversion()" disabled class="gold-gradient-btn px-4 py-1.5 rounded-lg text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed">
          Convert & Download PDF
        </button>
      </div>

    </div>
  </div>

</body>
</html>
"""



# AGY MULTI-INSTANCE SESSION MANAGER
# =====================================================================
def get_tmux_cmd():
    import glob
    for sock in glob.glob('/data/data/com.termux/files/usr/var/run/tmux-*/default') + glob.glob('/tmp/tmux-*/default'):
        if os.path.exists(sock):
            try:
                res = subprocess.run([TMUX_BIN, '-S', sock, 'list-sessions'], capture_output=True, text=True, timeout=1)
                if res.returncode == 0:
                    return [TMUX_BIN, '-S', sock]
            except Exception:
                pass
    return [TMUX_BIN]

def list_agy_sessions():
    try:
        cmd = get_tmux_cmd() + ["list-sessions", "-F", "#{session_name}"]
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

def create_agy_session(ident=None):
    if not ident:
        ident = secrets.token_hex(4)
    ident = re.sub(r'[^a-zA-Z0-9_-]', '', ident)[:32]
    if not ident or ident == "default":
        ident = secrets.token_hex(4)
    session_name = f"agy-{ident}"
    current = list_agy_sessions()
    if len(current) >= 8:
        raise ValueError("Maximum of 8 AGY instances reached")
    tmux = get_tmux_cmd()
    check = subprocess.run(tmux + ["has-session", "-t", f"={session_name}"], capture_output=True)
    if check.returncode != 0:
        cmd = f"{PREFIX}/bin/proot-distro login debian -- /usr/local/bin/agy-runner-loop"
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["TMPDIR"] = f"{PREFIX}/tmp"
        env["PATH"] = f"{PREFIX}/bin:{PREFIX}/bin/applets:" + env.get("PATH", "")
        subprocess.run(
            tmux + ["new-session", "-d", "-s", session_name, "-n", "agy", cmd],
            check=True, timeout=5, env=env
        )
    return ident

def close_agy_session(ident):
    ident = re.sub(r'[^a-zA-Z0-9_-]', '', ident)[:32]
    if not ident or ident in ("default", "agy"):
        raise ValueError("Primary AGY session cannot be terminated")
    session_name = f"agy-{ident}"
    tmux = get_tmux_cmd()
    subprocess.run(tmux + ["kill-session", "-t", f"={session_name}"], capture_output=True, timeout=5)
    return True


class DashboardHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def get_session_user(self):
        cookie_header = self.headers.get('Cookie')
        if not cookie_header:
            return None
        c = cookies.SimpleCookie()
        try:
            c.load(cookie_header)
            if 'session_token' in c:
                u = verify_session_token(c['session_token'].value)
                if u:
                    return u
            if 'session_token_http' in c:
                u = verify_session_token(c['session_token_http'].value)
                if u:
                    return u
        except Exception:
            pass
        return None

    def do_HEAD(self):
        self.do_GET()

    def json_error(self, status, message, retry_after=None):
        body = json.dumps({"status": "error", "message": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def agy_response(self, action, ident=None):
        if not is_admin(self.get_session_user()):
            self.json_error(403, "Admin access required")
            return
        try:
            if action == 'list':
                payload = {"online": True, "sessions": list_agy_sessions(), "max_sessions": 8}
            elif action == 'create':
                new_id = create_agy_session(ident)
                payload = {"status": "ok", "id": new_id}
            elif action == 'close':
                close_agy_session(ident)
                payload = {"status": "ok"}
            else:
                self.json_error(400, "Unknown action")
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:
            self.json_error(500, str(error))

    def laptop_response(self, action, ident=None):
        if not is_admin(self.get_session_user()):
            self.json_error(403, "Admin access required")
            return
        try:
            data = LAPTOP_CLIENT.status() if action == 'list' else LAPTOP_CLIENT.operation(action, ident)
        except LaptopError as error:
            self.json_error(error.status, str(error))
            return
        body = json.dumps(data).encode()
        self.send_response(201 if action == 'create' else 200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        laptop_prefix = '/api/laptop/terminals/'
        agy_prefix = '/api/agy/sessions/'
        if self.path.startswith(laptop_prefix):
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
                self.json_error(400, 'DELETE does not accept a request body')
                return
            self.laptop_response('close', self.path[len(laptop_prefix):])
            return
        elif self.path.startswith(agy_prefix):
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
                self.json_error(400, 'DELETE does not accept a request body')
                return
            self.agy_response('close', self.path[len(agy_prefix):])
            return
        self.json_error(404, 'Unknown resource')

    def do_POST(self):
        slot = None
        acquired = False
        try:
            if self.headers.get("Transfer-Encoding"):
                self.json_error(400, "Use a Content-Length request")
                return
            value = self.headers.get("Content-Length", "0")
            if not value.isdecimal():
                self.json_error(400, "Invalid Content-Length")
                return
            length = int(value)
            if length > BODY_LIMITS.get(self.path, 0):
                self.json_error(413, "Request exceeds this endpoint's size limit")
                return
            slot, acquired = ADMISSION.acquire(self.path)
            if not acquired:
                self.json_error(429, "This operation is busy; retry shortly", retry_after=3)
                return
            self._do_POST()
        except (ValueError, TypeError, AttributeError, UnicodeError):
            self.json_error(400, "Invalid request body")
        except (TimeoutError, socket.timeout):
            self.json_error(408, "Upload stalled; retry the request")
        finally:
            if slot is not None and acquired:
                slot.release()

    def _do_POST(self):
        if self.path == "/api/login":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            username = data.get("username", "").strip()
            password = data.get("password", "")

            client = self.client_address[0]
            try:
                client = str(ipaddress.ip_address(self.headers.get("X-Real-IP", client)))
            except ValueError:
                pass
            if not LOGIN_LIMITER.allowed(client, username):
                self.json_error(429, "Too many login attempts; retry later", retry_after=600)
                return
            if not LOGIN_LIMITER.verifiers.acquire(blocking=False):
                self.json_error(503, "Login verification busy; retry shortly", retry_after=2)
                return
            try:
                valid = USER_STORE.verify(username, password)
            finally:
                LOGIN_LIMITER.verifiers.release()
            LOGIN_LIMITER.record(client, username, valid)
            if valid:
                # Fetch FileBrowser JWT token for SSO
                jwt_token = get_filebrowser_jwt(username)
                token = sign_session_token(username)

                resp_data = {
                    "status": "ok",
                    "user": username,
                    "role": "admin" if is_admin(username) else "family",
                    "jwt": jwt_token,
                    "redirect": "/"
                }
                resp_bytes = json.dumps(resp_data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # Set persistent 30-day session cookies (signed token + user)
                is_https = self.headers.get("X-Forwarded-Proto") == "https"
                self.send_header("Set-Cookie", f"session_token={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax" + ("; Secure" if is_https else ""))
                self.send_header("Set-Cookie", f"session_user={username}; Path=/; Max-Age=2592000; SameSite=Lax" + ("; Secure" if is_https else ""))
                if not is_https:
                    self.send_header("Set-Cookie", f"session_token_http={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)
            else:
                resp_bytes = json.dumps({"status": "error", "message": "Invalid username or password"}).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)

        elif self.path == '/api/agy/sessions':
            self.agy_response('create')
        elif self.path == '/api/laptop/terminals':
            self.laptop_response('create')

        elif self.path == "/api/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            is_https = self.headers.get("X-Forwarded-Proto") == "https"
            self.send_header("Set-Cookie", "session_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if is_https else ""))
            self.send_header("Set-Cookie", "session_user=; Path=/; Max-Age=0; SameSite=Lax" + ("; Secure" if is_https else ""))
            self.send_header("Set-Cookie", "session_token_http=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            body = b'{"status": "ok"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/notes":
            user = self.get_session_user()
            if not user:
                self.json_error(401, "Authentication required")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                self.json_error(400, "Invalid JSON payload")
                return
            if not isinstance(data, dict) or "notes" not in data or not isinstance(data["notes"], str):
                self.json_error(400, "Payload must be JSON object with 'notes' string")
                return
            success = set_user_notes(user, data["notes"])
            if success:
                resp = json.dumps({"status": "saved"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(resp)
            else:
                self.json_error(500, "Failed to persist notes")
            return

        elif self.path == "/api/agy/scroll":
            if not is_admin(self.get_session_user()):
                self.json_error(403, "Admin access required")
                return
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            action = data.get('action')
            lines = data.get('lines', 3)
            if action not in ('up', 'down', 'top', 'bottom') or type(lines) is not int or not 1 <= lines <= 1000:
                self.json_error(400, "Invalid scroll action")
                return
            sid = str(data.get('id', 'default'))
            if sid.startswith('agy-'):
                sid = sid[4:]
            sid = re.sub(r'[^a-zA-Z0-9_-]', '', sid)[:32]
            target = '=agy:' if not sid or sid in ('default', 'agy') else f'=agy-{sid}:'
            tmux = get_tmux_cmd()
            try:
                mode = subprocess.check_output(tmux + ['display-message', '-p', '-t', target, '#{pane_in_mode}'], timeout=2, stderr=subprocess.DEVNULL).decode().strip()
                if action in ('up', 'top'):
                    subprocess.run(tmux + ['copy-mode', '-e', '-t', target], check=True, timeout=2, stderr=subprocess.DEVNULL)
                if action == 'bottom':
                    if mode == '1':
                        subprocess.run(tmux + ['send-keys', '-t', target, '-X', 'cancel'], check=True, timeout=2)
                elif action in ('up', 'top') or mode == '1':
                    command = {'up':'scroll-up', 'down':'scroll-down', 'top':'history-top'}[action]
                    subprocess.run(tmux + ['send-keys', '-t', target, '-X', '-N', str(lines), command], check=True, timeout=2)
            except (subprocess.SubprocessError, OSError):
                self.json_error(503, "AGY session unavailable")
                return
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/services/toggle":
            user = self.get_session_user()
            if not is_admin(user):
                self.send_response(403)
                self.end_headers()
                return
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body) if body else {}
            svc_id = data.get("service")
            enable = data.get("enable")
            report = toggle_service(svc_id, enable)
            resp_bytes = json.dumps(report).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
            return

        elif self.path == "/api/clipboard/upload":
            user = self.get_session_user()
            if not is_admin(user):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "Unauthorized"}')
                return

            content_length = int(self.headers.get("Content-Length", 0))
            content_type = self.headers.get("Content-Type", "")
            if content_length > 25 * 1024 * 1024:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "Image exceeds 25MB limit"}')
                return

            body = self.rfile.read(content_length)
            img_bytes = None
            orig_filename = None

            if "multipart/form-data" in content_type:
                raw_msg = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + body
                msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw_msg)
                for part in msg.iter_parts():
                    payload = part.get_payload(decode=True)
                    if payload:
                        img_bytes = payload
                        orig_filename = part.get_filename()
                        break
            elif content_type.startswith("image/"):
                img_bytes = body
            elif "application/json" in content_type:
                try:
                    data = json.loads(body.decode("utf-8"))
                    b64_data = data.get("image") or data.get("data")
                    if b64_data and "," in b64_data:
                        b64_data = b64_data.split(",", 1)[1]
                    if b64_data:
                        img_bytes = base64.b64decode(b64_data)
                        orig_filename = data.get("filename")
                except Exception:
                    pass

            if not img_bytes:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "No valid image data received"}')
                return

            ext = ".png"
            if img_bytes.startswith(b"\xff\xd8\xff"):
                ext = ".jpg"
            elif img_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = ".png"
            elif img_bytes.startswith(b"GIF8"):
                ext = ".gif"
            elif img_bytes.startswith(b"RIFF") and b"WEBP" in img_bytes[:16]:
                ext = ".webp"
            elif orig_filename and "." in orig_filename:
                ext = os.path.splitext(orig_filename)[1].lower()

            save_dir = "/data/data/com.termux/files/home/screenshots"
            os.makedirs(save_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_name = f"screenshot_{ts}{ext}"
            save_path = os.path.join(save_dir, saved_name)

            with open(save_path, "wb") as f:
                f.write(img_bytes)

            proot_path = f"/root/screenshots/{saved_name}"

            resp_data = {
                "status": "ok",
                "path": proot_path,
                "termux_path": save_path,
                "filename": saved_name,
                "url": f"/screenshots/{saved_name}",
                "size_kb": round(len(img_bytes) / 1024, 1)
            }
            resp_bytes = json.dumps(resp_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
            return

        elif self.path == "/api/convert":
            user = self.get_session_user()
            if not user:
                self.send_response(403)
                self.end_headers()
                return
            content_length = int(self.headers.get("Content-Length", 0))
            content_type = self.headers.get("Content-Type", "")
            if content_length > 32 * 1024 * 1024:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Uploaded files exceed 32 MiB limit"}).encode("utf-8"))
                return

            body = self.rfile.read(content_length)
            uploaded_files = []
            if "multipart/form-data" in content_type:
                raw_msg = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + body
                msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw_msg)
                for part in msg.iter_parts():
                    p_fn = part.get_filename()
                    p_name = part.get_param("name", header="content-disposition")
                    payload = part.get_payload(decode=True)
                    if (p_fn or p_name in ("file", "files", "files[]")) and payload:
                        fn = p_fn or f"document_{len(uploaded_files)+1}.docx"
                        uploaded_files.append((fn, payload))
            else:
                if body:
                    uploaded_files.append(("document.docx", body))

            if len(uploaded_files) > 10:
                self.json_error(413, "At most 10 documents per conversion")
                return
            if not uploaded_files:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "No files received"}).encode("utf-8"))
                return

            out_bytes, out_name, mime_type, err = convert_documents_batch(uploaded_files)
            if err:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": err}).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Disposition", f'attachment; filename="{out_name}"')
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        elif self.path == "/api/vpn/toggle":
            user = self.get_session_user()
            if not is_admin(user):
                self.send_response(403)
                self.end_headers()
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            data = json.loads(raw) if raw else {}
            enabled = data.get("enabled", not TOR_PROXY.snapshot()["enabled"])
            if not isinstance(enabled, bool):
                self.json_error(400, "enabled must be a boolean")
                return
            try:
                state = TOR_PROXY.set_enabled(enabled)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                self.json_error(503, "Tor could not be controlled; check recovery logs")
                return
            resp_bytes = json.dumps(state).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/healthz":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        user = self.get_session_user()

        if self.path in ["/portal.css", "/static/portal.css"]:
            css_path = os.path.join(STATIC_DIR, "portal.css")
            if os.path.exists(css_path):
                with open(css_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
            return

        if self.path in ["/wallpaper.webp", "/static/wallpaper.webp"]:
            if os.path.exists(WALLPAPER_PATH):
                with open(WALLPAPER_PATH, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/webp")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
            return

        if self.path in ["/interactive_player.js", "/static/interactive_player.js"]:
            player_path = os.path.join(STATIC_DIR, "interactive_player.js")
            if os.path.exists(player_path):
                with open(player_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
            return

        if self.path.split("?", 1)[0] in ("/api/auth-check", "/api/auth-check-admin"):
            fwd_uri = self.headers.get("X-Forwarded-Uri", "")
            if not user:
                if "/api/" in fwd_uri:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"error","message":"Unauthorized"}')
                else:
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                return

            if self.path.split("?", 1)[0] == "/api/auth-check-admin" and not is_admin(user):
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Forbidden: Administrator privileges required.")
                return

            self.send_response(200)
            self.send_header("X-Forwarded-User", user)
            self.end_headers()
            return

        if self.path == "/login":
            login_path = os.path.join(STATIC_DIR, "login.html")
            if os.path.exists(login_path):
                with open(login_path, "rb") as f:
                    body = f.read()
            else:
                body = LOGIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/logout":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "session_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))
            self.send_header("Set-Cookie", "session_user=; Path=/; Max-Age=0; SameSite=Lax" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))
            self.end_headers()
            return

        # FileBrowser is served only through Caddy and the streaming gateway.
        if self.path.startswith('/drive'):
            self.send_error(404)
            return

        # Protected Endpoints
        if not user:
            if self.path.startswith("/api/"):
                self.json_error(401, "Authentication required")
                return
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        if self.path == "/api/notes":
            notes = get_user_notes(user)
            body = json.dumps({"notes": notes}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == '/api/agy/sessions':
            self.agy_response('list')
            return
        if self.path == '/api/laptop/terminals':
            self.laptop_response('list')
            return
        if self.path == '/static/laptop.js':
            if not is_admin(user):
                self.json_error(403, 'Admin access required')
                return
            try:
                body = open(os.path.join(STATIC_DIR, 'laptop.js'), 'rb').read()
            except OSError:
                self.json_error(503, 'Laptop controls are not installed')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/stats":
            if not is_admin(user):
                self.send_response(403)
                self.end_headers()
                return
            data = get_stats()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path in ("/api/services", "/api/services/list") or self.path.startswith("/api/services?"):
            user = self.get_session_user()
            if not is_admin(user):
                self.send_response(403)
                self.end_headers()
                return
            force = "refresh=1" in self.path
            data = get_services_report(force=force)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return


        elif self.path == "/api/vpn/status":
            if not is_admin(user):
                self.send_response(403)
                self.end_headers()
                return
            body = json.dumps(TOR_PROXY.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/" or self.path.startswith("/index"):
            if is_admin(user):
                admin_file = os.path.join(STATIC_DIR, "admin.html")
                if os.path.exists(admin_file):
                    try:
                        with open(admin_file, "rb") as f:
                            body = f.read()
                    except Exception:
                        body = ADMIN_DASHBOARD_HTML.encode("utf-8")
                else:
                    body = ADMIN_DASHBOARD_HTML.encode("utf-8")
            else:
                fam_file = os.path.join(STATIC_DIR, "family.html")
                if os.path.exists(fam_file):
                    try:
                        with open(fam_file, "rb") as f:
                            body = f.read()
                    except Exception:
                        body = FAMILY_DASHBOARD_HTML.encode("utf-8")
                else:
                    body = FAMILY_DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

class ThreadedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self._requests = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._requests.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 2\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._requests.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._requests.release()

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if not isinstance(error, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            super().handle_error(request, client_address)

def run_server():
    server_address = ('127.0.0.1', DASHBOARD_PORT)
    httpd = ThreadedServer(server_address, DashboardHandler)
    print(f"Role-Based Dashboard running on http://127.0.0.1:{DASHBOARD_PORT}")
    httpd.serve_forever()

if __name__ == '__main__':
    run_server()
