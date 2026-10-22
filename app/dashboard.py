#!/usr/bin/env python3
import json
import sys
import os
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
MOBILE_LIB = os.path.expanduser("~/.local/lib/mobile-server")
sys.path.insert(0, MOBILE_LIB if os.path.isdir(MOBILE_LIB) else os.path.dirname(__file__))
from auth import CredentialStore, LoginLimiter
from telemetry import Telemetry
from limits import Admission, BODY_LIMITS
from tor_proxy import TorProxy
TOR_PROXY = TorProxy()
USER_STORE = CredentialStore()
LOGIN_LIMITER = LoginLimiter()
ADMISSION = Admission()
TELEMETRY = Telemetry()

def is_admin(username):
    return USER_STORE.is_admin(username)


DASHBOARD_PORT = 8001
TAILSCALE_IP = "100.75.159.53"
LOCAL_IP = "192.168.1.14"
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
            "http://127.0.0.1:8080/drive/api/login",
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
    return TELEMETRY.snapshot()


# =====================================================================
# SYSTEM SERVICE & DYNAMIC RAM MANAGER
# =====================================================================
SERVICES_CONFIG = {
    "filebrowser": {
        "id": "filebrowser",
        "name": "Cloud Storage & Media",
        "category": "Storage & Media",
        "port": 8080,
        "pattern": "filebrowser -d",
        "start_cmd": ["proot-distro", "login", "debian", "--bind", "/storage/emulated/0:/storage/emulated/0", "--", "filebrowser", "-d", "/data/data/com.termux/files/home/.config/filebrowser/filebrowser.db", "-a", "127.0.0.1", "-c", "/data/data/com.termux/files/home/.config/filebrowser/fb_config.json"],
        "icon": "folder",
        "desc": "Family Cloud Storage, Photos, Music & Google Files-style Media Engine",
        "open_url": "/drive/",
        "open_label": "Master Drive",
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

def get_services_report():
    services = []
    total_active_mem = 0.0
    services_sleeping = 0
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

        pids = get_service_pids(cfg["pattern"])
        running = len(pids) > 0
        rss_values = [get_process_rss(pid) for pid in pids]
        rss_mb = round(sum(rss_values), 1) if running and all(v is not None for v in rss_values) else None if running else 0.0
        if running:
            total_active_mem += rss_mb or 0
        else:
            services_sleeping += 1

        services.append({
            "id": sid,
            "name": cfg["name"],
            "category": cfg.get("category", "General"),
            "running": running,
            "ondemand": False,
            "pid": pids[0] if running else None,
            "rss_mb": rss_mb,
            "status_badge": f"Active ({rss_mb} MB)" if running else "Sleeping (0 MB)",
            "port": cfg.get("port"),
            "open_url": cfg.get("open_url"),
            "open_label": cfg.get("open_label"),
            "desc": cfg["desc"],
            "can_toggle": cfg.get("can_toggle", True)
        })

    return {
        "services": services,
        "total_active_mem_mb": round(total_active_mem, 1) if all(v["rss_mb"] is not None for v in services) else None,
        "estimated_saved_mem_mb": None,
        "sleeping_count": services_sleeping
    }

def toggle_service(sid, enable=None):
    cfg = SERVICES_CONFIG.get(sid)
    if not cfg:
        return {"status": "error", "message": "Unknown service"}
    if cfg.get("is_ondemand"):
        return get_services_report()
    target = not bool(get_service_pids(cfg["pattern"])) if enable is None else bool(enable)
    try:
        result = subprocess.run([
            "python3", os.path.expanduser("~/bin/server_supervisor.py"),
            "--service", sid, "--enable", str(target).lower()
        ], capture_output=True, text=True, timeout=25)
        if result.returncode:
            return {"status": "error", "message": "Service control failed; see server recovery logs"}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "error", "message": "Service control timed out; see server recovery logs"}
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
  <title>Login &bull; K20 Pro Cloud Server</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background: #090807;
      color: #fdfaf6;
      min-height: 100vh;
      overflow-x: hidden;
    }
    .mono { font-family: 'JetBrains Mono', monospace; }
    .bg-wallpaper {
      background-image: url('/wallpaper.webp');
      background-size: cover;
      background-position: center;
      background-attachment: fixed;
    }
    .golden-glass {
      background: rgba(14, 12, 10, 0.72);
      backdrop-filter: blur(32px) saturate(180%);
      -webkit-backdrop-filter: blur(32px) saturate(180%);
      border: 1px solid rgba(245, 158, 11, 0.22);
      box-shadow: 0 25px 60px -15px rgba(0, 0, 0, 0.8), 0 0 30px -10px rgba(245, 158, 11, 0.15);
    }
    .gold-gradient-btn {
      background: linear-gradient(135deg, #f59e0b 0%, #fbbf24 50%, #d97706 100%);
      color: #0c0a09;
      box-shadow: 0 8px 24px -6px rgba(245, 158, 11, 0.5);
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .gold-gradient-btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 14px 30px -6px rgba(245, 158, 11, 0.65);
      filter: brightness(1.08);
    }
    .gold-input {
      background: rgba(22, 18, 14, 0.65);
      border: 1px solid rgba(245, 158, 11, 0.25);
      color: #fff;
      transition: all 0.2s ease;
    }
    .gold-input:focus {
      outline: none;
      border-color: #f59e0b;
      box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.25);
      background: rgba(28, 22, 16, 0.85);
    }
  </style>
</head>
<body class="bg-wallpaper flex items-center justify-center p-4 min-h-screen">
  <div class="fixed inset-0 bg-black/45 backdrop-blur-md pointer-events-none"></div>

  <div class="relative z-10 w-full max-w-md">
    <div class="golden-glass p-8 sm:p-10 rounded-3xl">
      <!-- Header -->
      <div class="text-center space-y-3 mb-8">
        <div class="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-amber-500/15 border border-amber-500/30 text-amber-400 mb-1">
          <svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path>
          </svg>
        </div>
        <h1 class="text-2xl sm:text-3xl font-extrabold text-white tracking-tight">K20 Pro Cloud</h1>
        <p class="text-xs sm:text-sm text-amber-200/70 font-medium">Enter your credentials to access your cloud storage</p>
      </div>

      <!-- Form -->
      <form id="login-form" onsubmit="handleLogin(event)" class="space-y-4">
        <div>
          <label class="block text-xs font-bold uppercase tracking-wider text-amber-300/80 mb-1.5">Username</label>
          <input type="text" id="username" required value="" class="gold-input w-full px-4 py-3 rounded-xl text-sm" placeholder="Username" autocomplete="username">
        </div>

        <div>
          <label class="block text-xs font-bold uppercase tracking-wider text-amber-300/80 mb-1.5">Password</label>
          <input type="password" id="password" required value="" class="gold-input w-full px-4 py-3 rounded-xl text-sm" placeholder="Password" autocomplete="current-password">
        </div>

        <div id="error-msg" class="hidden p-3 rounded-xl bg-red-500/15 border border-red-500/30 text-red-300 text-xs text-center font-medium"></div>

        <button type="submit" id="submit-btn" class="gold-gradient-btn w-full py-3.5 rounded-xl font-bold text-sm flex items-center justify-center gap-2 mt-2">
          <span>Sign In</span>
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
        </button>
      </form>

      <!-- Footer Info -->
      <div class="mt-8 pt-5 border-t border-amber-500/15 text-center text-[11px] text-amber-200/40">
        <span>Protected by WireGuard Encryption</span>
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
          // Store FileBrowser JWT for instant SSO in browser
          if (data.jwt) {
            localStorage.setItem('jwt', data.jwt);
          }
          window.location.href = data.redirect || '/';
        } else {
          errBox.innerText = data.message || 'Invalid username or password';
          errBox.classList.remove('hidden');
          btn.disabled = false;
          btn.innerHTML = '<span>Sign In to Dashboard</span><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>';
        }
      } catch (err) {
        errBox.innerText = 'Server connection error. Please try again.';
        errBox.classList.remove('hidden');
        btn.disabled = false;
        btn.innerHTML = '<span>Sign In to Dashboard</span>';
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
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background: #090807;
      color: #fdfaf6;
      min-height: 100vh;
      overflow-x: hidden;
    }
    .mono { font-family: 'JetBrains Mono', monospace; }
    .bg-wallpaper {
      background-image: url('/wallpaper.webp');
      background-size: cover;
      background-position: center;
      background-attachment: fixed;
    }
    .golden-glass-card {
      background: rgba(14, 12, 10, 0.65);
      backdrop-filter: blur(28px) saturate(180%);
      -webkit-backdrop-filter: blur(28px) saturate(180%);
      border: 1px solid rgba(245, 158, 11, 0.22);
      box-shadow: 0 20px 50px -10px rgba(0, 0, 0, 0.75), 0 0 25px -10px rgba(245, 158, 11, 0.12);
      transition: all 0.35s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .golden-glass-card:hover {
      transform: translateY(-4px);
      border-color: rgba(245, 158, 11, 0.45);
      box-shadow: 0 25px 60px -10px rgba(0, 0, 0, 0.85), 0 0 35px -5px rgba(245, 158, 11, 0.25);
    }
    .gold-gradient-btn {
      background: linear-gradient(135deg, #f59e0b 0%, #fbbf24 50%, #d97706 100%);
      color: #0c0a09;
      box-shadow: 0 8px 24px -6px rgba(245, 158, 11, 0.5);
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .gold-gradient-btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 14px 30px -6px rgba(245, 158, 11, 0.65);
      filter: brightness(1.08);
    }
  </style>
</head>
<body class="bg-wallpaper p-4 sm:p-8 min-h-screen">
  <div class="fixed inset-0 bg-black/40 backdrop-blur-sm pointer-events-none"></div>

  <div class="relative z-10 max-w-5xl mx-auto space-y-8">
    <!-- Header -->
    <header class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 pb-6 border-b border-amber-500/20">
      <div class="flex items-center gap-3.5">
        <div class="w-12 h-12 rounded-2xl bg-amber-500/15 border border-amber-500/30 flex items-center justify-center text-amber-400">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 00-9.78 2.096A4.001 4.001 0 003 15z"></path></svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <span class="text-xs font-bold tracking-widest uppercase text-amber-400">Private Cloud</span>
            <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">Family Portal</span>
          </div>
          <h1 class="text-2xl font-extrabold text-white tracking-tight">Welcome Home</h1>
        </div>
      </div>

      <div class="flex items-center gap-3">
        <div class="px-3 py-1.5 rounded-xl bg-amber-950/40 border border-amber-500/20 text-xs text-amber-200/80 font-medium">
          Logged in as <span class="font-bold text-amber-300">Family</span>
        </div>
        <button onclick="handleLogout()" class="px-3.5 py-1.5 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-xs text-amber-100 font-bold transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path></svg>
          <span>Sign Out</span>
        </button>
      </div>
    </header>

    <!-- Welcome Banner -->
    <div class="p-6 rounded-3xl bg-amber-500/10 border border-amber-500/25 flex flex-col sm:flex-row items-center justify-between gap-4">
      <div class="space-y-1">
        <h2 class="text-lg font-bold text-amber-200">Your Safe Family Vault</h2>
        <p class="text-xs text-amber-100/70">Easily upload and access your family photos, documents, and favorite music from anywhere.</p>
      </div>
      <div class="flex items-center gap-2 text-xs font-bold text-emerald-400 bg-emerald-500/10 px-3 py-1.5 rounded-xl border border-emerald-500/20">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
        <span>Storage Online & Encrypted</span>
      </div>
    </div>

    <!-- 3 Family Services Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">

      <!-- 1. Personal Cloud Storage -->
      <div class="golden-glass-card p-6 sm:p-7 rounded-3xl flex flex-col justify-between gap-6">
        <div class="space-y-4">
          <div class="flex items-start justify-between">
            <div class="p-3.5 rounded-2xl bg-amber-500/15 border border-amber-500/30 text-amber-400">
              <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"></path></svg>
            </div>
            <span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">CLOUD DRIVE</span>
          </div>
          <div>
            <span class="text-[10px] font-bold uppercase tracking-wider text-amber-400/80">Family Files</span>
            <h3 class="text-xl font-bold text-white mt-0.5">Cloud Storage</h3>
            <p class="text-xs text-amber-100/70 mt-2 leading-relaxed">
              Upload, organize, and download documents, downloads, and important files safely.
            </p>
          </div>
        </div>
        <div class="pt-4 border-t border-amber-500/15">
          <a href="/drive/" class="gold-gradient-btn w-full py-3 rounded-xl text-xs font-bold flex items-center justify-center gap-2">
            <span>Open Cloud Drive</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        </div>
      </div>

      <!-- 2. Family Photos -->
      <div class="golden-glass-card p-6 sm:p-7 rounded-3xl flex flex-col justify-between gap-6">
        <div class="space-y-4">
          <div class="flex items-start justify-between">
            <div class="p-3.5 rounded-2xl bg-amber-500/15 border border-amber-500/30 text-amber-400">
              <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
            </div>
            <span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">ALBUMS</span>
          </div>
          <div>
            <span class="text-[10px] font-bold uppercase tracking-wider text-amber-400/80">Memories & Vacations</span>
            <h3 class="text-xl font-bold text-white mt-0.5">Family Photos</h3>
            <p class="text-xs text-amber-100/70 mt-2 leading-relaxed">
              Browse, view, and share full-resolution family photo albums and picture collections.
            </p>
          </div>
        </div>
        <div class="pt-4 border-t border-amber-500/15">
          <a href="/drive/files/Photos" class="gold-gradient-btn w-full py-3 rounded-xl text-xs font-bold flex items-center justify-center gap-2">
            <span>View Photos</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        </div>
      </div>

      <!-- 3. Music Jukebox -->
      <div class="golden-glass-card p-6 sm:p-7 rounded-3xl flex flex-col justify-between gap-6">
        <div class="space-y-4">
          <div class="flex items-start justify-between">
            <div class="p-3.5 rounded-2xl bg-amber-500/15 border border-amber-500/30 text-amber-400">
              <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"></path></svg>
            </div>
            <span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">AUDIO</span>
          </div>
          <div>
            <span class="text-[10px] font-bold uppercase tracking-wider text-amber-400/80">Hi-Fi Streaming</span>
            <h3 class="text-xl font-bold text-white mt-0.5">Music Jukebox</h3>
            <p class="text-xs text-amber-100/70 mt-2 leading-relaxed">
              Listen to songs, audiobooks, and family music playlists directly with built-in streaming.
            </p>
          </div>
        </div>
        <div class="pt-4 border-t border-amber-500/15">
          <a href="/drive/files/Music" class="gold-gradient-btn w-full py-3 rounded-xl text-xs font-bold flex items-center justify-center gap-2">
            <span>Open Music</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        </div>
      </div>

      <!-- 4. Document to PDF Converter -->
      <div class="golden-glass-card p-6 sm:p-7 rounded-3xl flex flex-col justify-between gap-6">
        <div class="space-y-4">
          <div class="flex items-start justify-between">
            <div class="p-3.5 rounded-2xl bg-amber-500/15 border border-amber-500/30 text-amber-400">
              <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>
            </div>
            <span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">TOOLS</span>
          </div>
          <div>
            <span class="text-[10px] font-bold uppercase tracking-wider text-amber-400/80">PDF Converter</span>
            <h3 class="text-xl font-bold text-white mt-0.5">Document to PDF</h3>
            <p class="text-xs text-amber-100/70 mt-2 leading-relaxed">
              Convert Word documents, presentations, spreadsheets, and pictures into PDF instantly.
            </p>
          </div>
        </div>
        <div class="pt-4 border-t border-amber-500/15">
          <button onclick="openConverterModal()" class="gold-gradient-btn w-full py-3 rounded-xl text-xs font-bold flex items-center justify-center gap-2">
            <span>Convert File to PDF</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </button>
        </div>
      </div>

    </div>

    <!-- Footer -->
    <footer class="pt-8 border-t border-amber-500/15 text-center text-xs text-amber-200/50">
      <span>HomeCloud Server &bull; Connected securely via Tailscale</span>
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
              <div class="flex items-center justify-between p-2.5 rounded-xl bg-black/40 border border-amber-500/20 text-xs">
                <div class="flex items-center gap-2.5 overflow-hidden">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 shrink-0">${escapeHtml(ext)}</span>
                  <div class="truncate">
                    <p class="font-medium text-white truncate max-w-[220px] sm:max-w-xs">${escapeHtml(file.name)}</p>
                    <p class="text-[10px] text-amber-100/50">${formatFileSize(file.size)}</p>
                  </div>
                </div>
                <button type="button" onclick="removeConvertFile(${idx})" class="p-1 rounded-lg text-amber-200/50 hover:text-rose-400 hover:bg-rose-500/10 transition shrink-0 ml-2">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
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
            dropzone.classList.add('border-amber-400', 'bg-amber-500/20');
          });
        });
        ['dragleave', 'drop'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.remove('border-amber-400', 'bg-amber-500/20');
          });
        });
        dropzone.addEventListener('drop', (e) => {
          if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            addConvertFiles(e.dataTransfer.files);
          }
        });
      }
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initConverter);
    } else {
      initConverter();
    }

    async function executeConversion() {
      if (!selectedConvertFiles.length) return;
      const progress = document.getElementById('convert-progress');
      const statusText = document.getElementById('convert-status-text');
      const btn = document.getElementById('convert-btn');
      progress.classList.remove('hidden');
      if (statusText) {
        statusText.innerText = selectedConvertFiles.length === 1
          ? "Converting document to PDF..."
          : `Batch converting ${selectedConvertFiles.length} files to PDF...`;
      }
      btn.disabled = true;

      const formData = new FormData();
      selectedConvertFiles.forEach(file => {
        formData.append('files', file, file.name);
      });

      try {
        const res = await fetch('/api/convert', { method: 'POST', body: formData });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ message: 'Conversion failed' }));
          alert('Error: ' + (err.message || 'Server error during conversion'));
          progress.classList.add('hidden');
          btn.disabled = false;
          return;
        }

        const disposition = res.headers.get('Content-Disposition') || '';
        let filename = selectedConvertFiles.length > 1 ? 'converted_documents.zip' : (selectedConvertFiles[0].name.replace(/\\.[^/.]+$/, "") + ".pdf");
        const match = disposition.match(/filename="?([^";]+)"?/);
        if (match && match[1]) {
          filename = match[1];
        }

        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();
        closeConverterModal();
      } catch (err) {
        alert('Upload/Conversion error: ' + err);
        progress.classList.add('hidden');
        btn.disabled = false;
      }
    }

  </script>

  <!-- Document to PDF Converter Modal -->
  <div id="converter-modal" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-lg hidden">
    <div class="golden-glass-card max-w-lg w-full p-6 sm:p-7 rounded-3xl space-y-4 border border-amber-500/40 shadow-2xl relative max-h-[90vh] flex flex-col justify-between">

      <!-- Modal Header -->
      <div class="flex items-start justify-between">
        <div class="flex items-center gap-3">
          <div class="p-2.5 rounded-2xl bg-amber-500/20 border border-amber-500/40 text-amber-300">
            <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>
          </div>
          <div>
            <h3 class="text-lg font-bold text-white">Batch Document to PDF</h3>
            <p class="text-xs text-amber-100/70">Convert 1 or multiple documents at once</p>
          </div>
        </div>
        <button onclick="closeConverterModal()" class="text-amber-200/60 hover:text-white p-1 rounded-xl hover:bg-white/5 transition">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>

      <!-- Hidden Multi-file input -->
      <input type="file" id="convert-file-input" class="sr-only" accept="*/*" multiple>

      <!-- Dropzone (shown when 0 files selected) -->
      <label for="convert-file-input" id="convert-dropzone" class="border-2 border-dashed border-amber-500/40 hover:border-amber-400/80 bg-amber-500/5 hover:bg-amber-500/10 transition rounded-2xl p-6 text-center cursor-pointer flex flex-col items-center justify-center space-y-3 block">
        <svg class="w-10 h-10 text-amber-400/80 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"></path></svg>
        <div class="pointer-events-none">
          <p class="text-sm font-semibold text-white">Choose multiple files or drag & drop</p>
          <p class="text-xs text-amber-100/50 mt-1">Word (.docx, .doc), PPT (.pptx), Excel (.xlsx), Images & Text</p>
        </div>
        <span class="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-amber-500/20 text-amber-300 border border-amber-500/40 text-xs font-semibold hover:bg-amber-500/30 transition pointer-events-none">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
          Select Files (Multi-select)
        </span>
      </label>

      <!-- Selected Files Container (shown when >= 1 files selected) -->
      <div id="convert-file-list-container" class="hidden space-y-3 flex-1 overflow-hidden flex flex-col">
        <div class="flex items-center justify-between text-xs pb-1.5 border-b border-amber-500/15">
          <span id="convert-file-summary" class="font-bold text-amber-300">0 files selected</span>
          <div class="flex items-center gap-2">
            <label for="convert-file-input" class="cursor-pointer text-[11px] px-2.5 py-1 rounded-lg bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 font-semibold border border-amber-500/30 transition flex items-center gap-1">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
              <span>+ Add More</span>
            </label>
            <button type="button" onclick="clearAllConvertFiles()" class="text-[11px] px-2.5 py-1 rounded-lg bg-white/5 text-amber-200/60 hover:text-white hover:bg-white/10 font-semibold transition">
              Clear All
            </button>
          </div>
        </div>
        <div id="convert-file-list" class="max-h-52 overflow-y-auto space-y-1.5 pr-1"></div>
      </div>

      <!-- Conversion Progress State -->
      <div id="convert-progress" class="hidden space-y-2">
        <div class="flex items-center justify-between text-xs">
          <span class="text-amber-300 font-medium flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
            <span id="convert-status-text">Converting documents to PDF...</span>
          </span>
          <span class="mono text-amber-400" id="convert-status-pct">Batch active</span>
        </div>
        <div class="w-full bg-black/40 h-1.5 rounded-full overflow-hidden border border-amber-500/20">
          <div class="h-full bg-gradient-to-r from-amber-500 to-amber-300 rounded-full animate-pulse w-full"></div>
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="pt-2 flex items-center justify-end gap-3 border-t border-amber-500/15">
        <button onclick="closeConverterModal()" class="px-4 py-2 rounded-xl text-xs font-semibold text-amber-200/80 hover:bg-white/5 transition">Cancel</button>
        <button id="convert-btn" onclick="executeConversion()" disabled class="gold-gradient-btn px-5 py-2.5 rounded-xl text-xs font-bold disabled:opacity-40 disabled:cursor-not-allowed">
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
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background: #090807;
      color: #fdfaf6;
      min-height: 100vh;
      overflow-x: hidden;
    }
    .mono { font-family: 'JetBrains Mono', monospace; }
    .bg-wallpaper {
      background-image: url('/wallpaper.webp');
      background-size: cover;
      background-position: center;
      background-attachment: fixed;
    }
    .golden-glass-card {
      background: rgba(14, 12, 10, 0.68);
      backdrop-filter: blur(28px) saturate(180%);
      -webkit-backdrop-filter: blur(28px) saturate(180%);
      border: 1px solid rgba(245, 158, 11, 0.22);
      box-shadow: 0 20px 50px -10px rgba(0, 0, 0, 0.75), 0 0 25px -10px rgba(245, 158, 11, 0.12);
      transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .golden-glass-card:hover {
      border-color: rgba(245, 158, 11, 0.45);
      box-shadow: 0 25px 60px -10px rgba(0, 0, 0, 0.85), 0 0 35px -5px rgba(245, 158, 11, 0.25);
    }
    .gold-gradient-btn {
      background: linear-gradient(135deg, #f59e0b 0%, #fbbf24 50%, #d97706 100%);
      color: #0c0a09;
      box-shadow: 0 8px 24px -6px rgba(245, 158, 11, 0.5);
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .gold-gradient-btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 14px 30px -6px rgba(245, 158, 11, 0.65);
      filter: brightness(1.08);
    }

    /* Switch styles */
    .switch {
      position: relative;
      display: inline-block;
      width: 44px;
      height: 24px;
    }
    .switch input {
      opacity: 0;
      width: 0;
      height: 0;
    }
    .slider {
      position: absolute;
      cursor: pointer;
      top: 0; left: 0; right: 0; bottom: 0;
      background-color: rgba(255, 255, 255, 0.12);
      border: 1px solid rgba(245, 158, 11, 0.25);
      transition: .3s;
      border-radius: 24px;
    }
    .slider:before {
      position: absolute;
      content: "";
      height: 18px;
      width: 18px;
      left: 2px;
      bottom: 2px;
      background-color: #fdfaf6;
      transition: .3s;
      border-radius: 50%;
      box-shadow: 0 2px 4px rgba(0,0,0,0.4);
    }
    input:checked + .slider {
      background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
      border-color: #f59e0b;
    }
    input:checked + .slider:before {
      transform: translateX(20px);
      background-color: #090807;
    }

  </style>
</head>
<body class="bg-wallpaper p-4 sm:p-8 min-h-screen">
  <div class="fixed inset-0 bg-black/45 backdrop-blur-sm pointer-events-none"></div>

  <div class="relative z-10 max-w-6xl mx-auto space-y-8">
    <!-- Header -->
    <header class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 pb-6 border-b border-amber-500/20">
      <div class="flex items-center gap-3.5">
        <div class="w-12 h-12 rounded-2xl bg-amber-500/15 border border-amber-500/30 flex items-center justify-center text-amber-400">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <span class="text-xs font-bold tracking-widest uppercase text-amber-400">Master Console</span>
            <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/35">Admin: astro</span>
          </div>
          <h1 class="text-2xl font-extrabold text-white tracking-tight">K20 Pro Server Manager</h1>
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-3">
        <div class="px-3 py-1.5 rounded-xl bg-amber-950/40 border border-amber-500/20 text-xs text-amber-200/80 font-medium">
          TS: <span id="header-ts-ip" class="mono font-bold text-amber-300">100.75.159.53</span> &bull;
          Wi-Fi: <span id="header-local-ip" class="mono font-bold text-amber-300">192.168.1.14</span> &bull;
          Uptime: <span id="header-uptime" class="mono font-bold text-white">...</span>
        </div>
        <button onclick="handleLogout()" class="px-3.5 py-1.5 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-xs text-amber-100 font-bold transition flex items-center gap-1.5">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"></path></svg>
          <span>Sign Out</span>
        </button>
      </div>
    </header>

    <!-- Telemetry Cards -->
    <section class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <!-- Battery -->
      <div class="golden-glass-card p-5 rounded-2xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-amber-300/70 uppercase tracking-wider">Battery</span>
          <span id="battery-status-badge" class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">...</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-2">
            <span id="battery-pct" class="text-3xl font-extrabold text-white">...%</span>
            <span id="battery-temp" class="text-xs mono text-amber-400">...°C</span>
          </div>
          <div class="w-full bg-neutral-900/80 rounded-full h-1.5 mt-2.5 overflow-hidden border border-amber-500/20">
            <div id="battery-bar" class="bg-amber-400 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>
        <span class="text-[10px] text-amber-200/50">Built-in Hardware UPS</span>
      </div>

      <!-- RAM -->
      <div class="golden-glass-card p-5 rounded-2xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-amber-300/70 uppercase tracking-wider">RAM Memory</span>
          <span class="text-xs mono text-amber-400" id="ram-pct">...%</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-1.5">
            <span id="ram-used" class="text-3xl font-extrabold text-white">...</span>
            <span class="text-xs text-amber-200/60">/ <span id="ram-total">N/A</span> GB</span>
          </div>
          <div class="w-full bg-neutral-900/80 rounded-full h-1.5 mt-2.5 overflow-hidden border border-amber-500/20">
            <div id="ram-bar" class="bg-amber-400 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>
        <span class="text-[10px] text-amber-200/50">LPDDR4X High-Speed</span>
      </div>

      <!-- Storage -->
      <div class="golden-glass-card p-5 rounded-2xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-amber-300/70 uppercase tracking-wider">UFS Storage</span>
          <span class="text-xs mono text-amber-400" id="storage-pct">...%</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-1.5">
            <span id="storage-free" class="text-3xl font-extrabold text-white">...</span>
            <span class="text-xs text-amber-200/60">GB free</span>
          </div>
          <div class="w-full bg-neutral-900/80 rounded-full h-1.5 mt-2.5 overflow-hidden border border-amber-500/20">
            <div id="storage-bar" class="bg-amber-400 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>
        <span class="text-[10px] text-amber-200/50">UFS 2.1 Fast Flash</span>
      </div>

      <!-- CPU -->
      <div class="golden-glass-card p-5 rounded-2xl flex flex-col justify-between">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-amber-300/70 uppercase tracking-wider">Snapdragon 855</span>
          <span class="text-xs mono text-amber-400" id="cpu-temp">...°C</span>
        </div>
        <div class="my-3">
          <div class="flex items-baseline gap-2">
            <span class="text-2xl font-extrabold text-white">8 Cores</span>
            <span id="cpu-load" class="text-xs mono text-amber-200/60">Load: ...</span>
          </div>
          <div class="w-full bg-neutral-900/80 rounded-full h-1.5 mt-2.5 overflow-hidden border border-amber-500/20">
            <div id="cpu-bar" class="bg-amber-400 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>
        <span class="text-[10px] text-amber-200/50">Kryo 485 Architecture</span>
      </div>
    </section>

    <!-- Live Network Monitor -->
    <section class="golden-glass-card p-6 rounded-3xl space-y-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2.5">
          <div class="p-2.5 rounded-xl bg-amber-500/15 border border-amber-500/30 text-amber-400">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.111 16.404a5.5 5.5 0 017.778 0M12 20h.01m-7.08-7.071c3.904-3.905 10.236-3.905 14.141 0M1.394 9.393c5.857-5.857 15.355-5.857 21.213 0"></path></svg>
          </div>
          <div>
            <h3 class="font-bold text-white text-base">Live Network & Bandwidth Monitor</h3>
            <p class="text-xs text-amber-200/60">Measured every 30 seconds; unavailable readings show N/A</p>
          </div>
        </div>
        <span class="flex items-center gap-1.5 px-3 py-1 rounded-full text-[11px] font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-ping"></span><span id="telemetry-state">WAITING FOR SAMPLE</span>
        </span>
      </div>

      <div class="grid grid-cols-2 sm:grid-cols-4 gap-4 pt-2">
        <!-- Wi-Fi Link Speed -->
        <div class="p-4 rounded-2xl bg-amber-950/30 border border-amber-500/15">
          <span class="text-[10px] font-bold uppercase text-amber-400/70 tracking-wider">Wi-Fi Link Speed</span>
          <div class="text-2xl font-extrabold text-white mt-1">
            <span id="net-wifi-speed">N/A</span> <span class="text-xs text-amber-200/60">Mbps</span>
          </div>
          <div class="text-[11px] text-amber-300/60 mt-1">Interface: <span class="mono">wlan0</span></div>
        </div>

        <!-- Signal Strength -->
        <div class="p-4 rounded-2xl bg-amber-950/30 border border-amber-500/15">
          <span class="text-[10px] font-bold uppercase text-amber-400/70 tracking-wider">Signal (RSSI)</span>
          <div class="text-2xl font-extrabold text-white mt-1">
            <span id="net-wifi-rssi">N/A</span> <span class="text-xs text-amber-200/60">dBm</span>
          </div>
          <div class="text-[11px] text-emerald-400 mt-1 font-medium">Measured RSSI; no assumed signal rating</div>
        </div>

        <!-- WAN Ping (Cloudflare) -->
        <div class="p-4 rounded-2xl bg-amber-950/30 border border-amber-500/15">
          <span class="text-[10px] font-bold uppercase text-amber-400/70 tracking-wider">Internet Latency</span>
          <div class="text-2xl font-extrabold text-white mt-1">
            <span id="net-wan-ping">N/A</span> <span class="text-xs text-amber-200/60">ms</span>
          </div>
          <div class="text-[11px] text-amber-300/60 mt-1">Target: Cloudflare 1.1.1.1</div>
        </div>

        <!-- DNS Filter Status -->
        <div class="p-4 rounded-2xl bg-amber-950/30 border border-amber-500/15">
          <span class="text-[10px] font-bold uppercase text-amber-400/70 tracking-wider">AdGuard DNS</span>
          <div class="text-xl font-extrabold text-emerald-400 mt-1">Port 5353</div>
          <div class="text-[11px] text-emerald-300/70 mt-1 font-medium"><span id="adguard-state">Checking interface…</span></div>
        </div>
      </div>
    </section>

    <!-- Services Grid (Admin sees all) -->

    <!-- System Services & Dynamic RAM Manager -->
    <section class="space-y-4">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <div class="flex items-center gap-2">
            <h2 class="text-lg font-bold text-white tracking-tight">System Services & RAM Optimizer</h2>
            <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40">DYNAMIC</span>
          </div>
          <p class="text-xs text-amber-100/60 mt-0.5">Control background services; memory readings cover visible service processes</p>
        </div>

        <!-- Live RAM Preservation Summary Pill -->
        <div class="flex items-center gap-2 bg-black/40 border border-amber-500/25 px-3 py-1.5 rounded-2xl text-xs backdrop-blur-md">
          <div class="flex items-center gap-1.5 text-emerald-400">
            <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
            <span class="font-bold mono" id="svc-active-mem">-- MB</span> <span class="text-[10px] uppercase text-emerald-400/80">Active</span>
          </div>
          <span class="text-amber-500/40">|</span>
          <div class="flex items-center gap-1.5 text-amber-300">
            <span class="w-2 h-2 rounded-full bg-amber-400"></span>
            <span class="font-bold mono" id="svc-saved-mem">-- MB</span> <span class="text-[10px] uppercase text-amber-300/80">Saved (unmeasured)</span>
            <span class="text-[10px] text-amber-100/50">(<span id="svc-sleeping-count">0</span> sleeping)</span>
          </div>
        </div>
      </div>

      <!-- Services Grid Container -->
      <div id="services-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
        <!-- Live rendered services -->
      </div>
    </section>
<section class="golden-glass-card p-6 sm:p-8 rounded-3xl space-y-6">
      <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div class="space-y-1">
          <div class="flex items-center gap-2">
            <span class="text-xs font-bold uppercase tracking-wider text-amber-400">Privacy & Tunneling</span>
            <span id="vpn-badge" class="px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">OFF</span>
          </div>
          <h2 class="text-xl font-bold text-white">Tor SOCKS Proxy</h2>
          <p class="text-xs text-amber-100/70 max-w-2xl">
            Only applications configured for this proxy use Tor. Full-device routing through Tor is unavailable on this setup. File transfers stay direct over Tailscale.
            On your laptop run <code class="block mt-2 select-all">ssh -N -L 9050:127.0.0.1:9050 -p 8022 u0_a505@100.75.159.53</code>
            Set your application's SOCKS5 proxy to 127.0.0.1:9050 with proxy DNS (SOCKS5h). Keep Tailscale addresses outside that proxy.
          </p>
        </div>

        <div class="flex items-center gap-3">
          <button id="vpn-toggle-btn" onclick="toggleVPN()" class="gold-gradient-btn px-5 py-2.5 rounded-xl text-xs font-bold flex items-center gap-2">
            <span id="vpn-btn-text">Enable Tor Proxy</span>
          </button>
        </div>
      </div>

      <!-- Network Topology Diagram -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 pt-4 border-t border-amber-500/15">
        <!-- Node 1: Client -->
        <div class="p-4 rounded-2xl bg-amber-950/30 border border-amber-500/20 space-y-1">
          <div class="text-[10px] font-bold text-amber-400/70 uppercase">Step 1: Configured Application</div>
          <div class="text-sm font-bold text-white">Application → local SOCKS5</div>
          <div class="text-xs text-amber-200/60 mono mt-1">WireGuard Mesh (Tailscale)</div>
          <div class="text-[11px] text-emerald-400 font-medium mt-2">✓ Encrypted End-to-End</div>
        </div>

        <!-- Node 2: K20 Pro Server -->
        <div class="p-4 rounded-2xl bg-amber-950/30 border border-amber-500/20 space-y-1">
          <div class="text-[10px] font-bold text-amber-400/70 uppercase">Step 2: K20 Pro Proxy</div>
          <div class="text-sm font-bold text-white">Tor client on server</div>
          <div class="text-xs text-amber-200/60 mono mt-1" id="server-direct-ip">Direct IP: checking…</div>
          <div class="text-[11px] text-amber-300 font-medium mt-2" id="server-gateway-status">Full-device routing unavailable</div>
        </div>

        <!-- Node 3: Outbound Exit -->
        <div class="p-4 rounded-2xl bg-amber-950/30 border border-amber-500/20 space-y-1">
          <div class="text-[10px] font-bold text-amber-400/70 uppercase">Step 3: Verified Tor Exit</div>
          <div class="text-sm font-bold text-white" id="dest-exit-name">Proxy disabled</div>
          <div class="text-xs text-amber-200/60 mono mt-1" id="dest-exit-ip">Proxy exit: unavailable</div>
          <div class="text-[11px] text-amber-400/80 font-medium mt-2" id="dest-exit-status">Only configured applications use this proxy</div>
        </div>
      </div>
    </section>

    <!-- Footer & Terminal Command Drawer -->
    <footer class="pt-6 border-t border-amber-500/15 flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-amber-200/50">
      <div class="flex items-center gap-3">
        <span>Powered by Snapdragon 855 &bull; Termux &bull; Caddy Reverse Proxy &bull; Tailscale</span>
      </div>
      <div class="flex items-center gap-4">
        <button onclick="copySSH()" class="hover:text-amber-300 transition underline underline-offset-4">Copy SSH Terminal Command</button>
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
      document.getElementById(id).style.width = typeof value === 'number' ? Math.min(100, Math.max(0, value)) + '%' : '0%';
    }
    async function fetchStats() {
      try {
        const res = await fetch('/api/stats');
        if (res.status === 401 || res.status === 403) { location.href = '/login'; return; }
        if (!res.ok) throw new Error('Telemetry unavailable');
        const data = await res.json();
        setText('telemetry-state', data.stale ? 'STALE / UNAVAILABLE' : 'MEASURED');
        setText('header-ts-ip', data.tailscale_ip);
        setText('header-local-ip', data.local_ip);
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
        setText('cpu-load', 'CPU: ' + metric(data.cpu_percent, '%') + ' · Load: ' + metric(data.load_avg?.[0]));
        setBar('cpu-bar', data.cpu_percent);
        setText('net-wifi-speed', metric(data.network?.wifi_speed));
        setText('net-wifi-rssi', metric(data.network?.rssi));
        setText('net-wan-ping', metric(data.network?.wan_ping));
        setText('adguard-state', data.network?.dns_status);
      } catch (err) { setText('telemetry-state', 'UNAVAILABLE'); }
    }
    let torEnabled = false;
    async function fetchVPN() {
      try {
        const res = await fetch('/api/vpn/status');
        if (!res.ok) throw new Error();
        updateVPNUI(await res.json());
      } catch (err) { setText('vpn-badge', 'STATUS UNAVAILABLE'); }
    }
    function updateVPNUI(data) {
      torEnabled = data.enabled === true;
      const verified = torEnabled && data.verified_via_tor === true && !data.stale;
      setText('vpn-badge', verified ? 'TOR EXIT VERIFIED' : torEnabled ? 'STARTING / UNVERIFIED' : 'OFF');
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

    function copySSH() {
      const cmd = "ssh -p 8022 u0_a505@100.75.159.53";
      navigator.clipboard.writeText(cmd);
      alert("Copied SSH command: " + cmd);
    }


    // Services & Memory Optimizer
    let servicesData = null;

    async function fetchServices() {
      try {
        const res = await fetch('/api/services');
        if (res.status === 401 || res.status === 403) return;
        const data = await res.json();
        servicesData = data;

        document.getElementById('svc-active-mem').innerText = metric(data.total_active_mem_mb, ' MB');
        document.getElementById('svc-saved-mem').innerText = 'N/A';
        document.getElementById('svc-sleeping-count').innerText = data.sleeping_count;

        renderServices(data.services);
      } catch (err) {
        console.error("Error fetching services:", err);
      }
    }

    function renderServices(services) {
      const grid = document.getElementById('services-grid');
      if (!grid) return;

      const icons = {
        'filebrowser': '<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"></path></svg>',
        'vaultwarden': '<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"></path></svg>',
        'adguard': '<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"></path></svg>',
        'terminal': '<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>',
        'agy': '<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>',
        'converter': '<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>',
        'syncthing': '<svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>'
      };

      grid.innerHTML = services.map(svc => {
        const isRunning = svc.running;
        const iconSvg = icons[svc.id] || icons['filebrowser'];
        const badgeClass = isRunning
          ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
          : 'bg-zinc-800/80 text-zinc-400 border border-zinc-700/50';

        const toggleHtml = svc.can_toggle ? `
          <label class="switch">
            <input type="checkbox" ${isRunning ? 'checked' : ''} onchange="toggleService('${svc.id}')">
            <span class="slider"></span>
          </label>
        ` : `
          <span class="text-[10px] font-bold mono uppercase px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30">Auto</span>
        `;

        const openBtn = `
          <a href="${svc.open_url}" ${svc.open_url.startsWith('http') || svc.open_url.startsWith('/') ? 'target="_blank"' : ''} class="gold-gradient-btn w-full py-2.5 rounded-xl text-xs font-bold flex items-center justify-center gap-1.5 ${!isRunning && svc.can_toggle ? 'opacity-40 pointer-events-none' : ''}">
            <span>${svc.open_label}</span>
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </a>
        `;

        return `
          <div class="golden-glass-card p-6 rounded-3xl flex flex-col justify-between gap-5 transition hover:border-amber-500/40">
            <div class="space-y-3">
              <div class="flex items-start justify-between">
                <div class="p-3 rounded-2xl bg-amber-500/10 border border-amber-500/20 text-amber-400">
                  ${iconSvg}
                </div>
                <div class="flex items-center gap-2.5">
                  <span class="px-2.5 py-0.5 rounded-full text-[10px] font-bold ${badgeClass}">
                    ${svc.status_badge}
                  </span>
                  ${toggleHtml}
                </div>
              </div>
              <div>
                <span class="text-[10px] font-bold uppercase tracking-widest text-amber-400/70">${svc.category}</span>
                <h3 class="font-bold text-base text-white mt-0.5">${svc.name}</h3>
                <p class="text-xs text-amber-100/60 mt-1 leading-relaxed">${svc.desc}</p>
                ${svc.id === 'vaultwarden' ? `
                  <div class="mt-2 p-2 rounded-xl bg-black/40 border border-amber-500/15 text-[10px] text-amber-200/80 flex items-center justify-between">
                    <span class="mono">Bitwarden App: http://100.75.159.53:8082</span>
                    <button onclick="navigator.clipboard.writeText('http://100.75.159.53:8082'); alert('Copied Bitwarden Server URL');" class="hover:text-white underline">Copy</button>
                  </div>
                ` : ''}
                ${svc.id === 'syncthing' ? `
                  <div class="mt-2 p-2 rounded-xl bg-black/40 border border-amber-500/15 text-[10px] text-amber-200/80 flex items-center justify-between">
                    <span class="mono">Sync Folder: PES/Sem3 &harr; PC</span>
                    <span class="text-amber-400 font-bold">Port 8384</span>
                  </div>
                ` : ''}
              </div>
            </div>
            <div class="pt-3 border-t border-amber-500/15">
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

    async function toggleService(sid) {

      try {
        const res = await fetch('/api/services/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ service: sid })
        });
        const data = await res.json();
        if (data.error) {
          alert(data.error);
        }
        if (data.services) {
          servicesData = data;
          document.getElementById('svc-active-mem').innerText = metric(data.total_active_mem_mb, ' MB');
          document.getElementById('svc-saved-mem').innerText = 'N/A';
          document.getElementById('svc-sleeping-count').innerText = data.sleeping_count;
          renderServices(data.services);
        }
      } catch (err) {
        alert('Failed to toggle service: ' + err);
      }
    }

    // Converter Modal
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
              <div class="flex items-center justify-between p-2.5 rounded-xl bg-black/40 border border-amber-500/20 text-xs">
                <div class="flex items-center gap-2.5 overflow-hidden">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 shrink-0">${escapeHtml(ext)}</span>
                  <div class="truncate">
                    <p class="font-medium text-white truncate max-w-[220px] sm:max-w-xs">${escapeHtml(file.name)}</p>
                    <p class="text-[10px] text-amber-100/50">${formatFileSize(file.size)}</p>
                  </div>
                </div>
                <button type="button" onclick="removeConvertFile(${idx})" class="p-1 rounded-lg text-amber-200/50 hover:text-rose-400 hover:bg-rose-500/10 transition shrink-0 ml-2">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
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
            dropzone.classList.add('border-amber-400', 'bg-amber-500/20');
          });
        });
        ['dragleave', 'drop'].forEach(name => {
          dropzone.addEventListener(name, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.remove('border-amber-400', 'bg-amber-500/20');
          });
        });
        dropzone.addEventListener('drop', (e) => {
          if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            addConvertFiles(e.dataTransfer.files);
          }
        });
      }
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initConverter);
    } else {
      initConverter();
    }

    async function executeConversion() {
      if (!selectedConvertFiles.length) return;
      const progress = document.getElementById('convert-progress');
      const statusText = document.getElementById('convert-status-text');
      const btn = document.getElementById('convert-btn');
      progress.classList.remove('hidden');
      if (statusText) {
        statusText.innerText = selectedConvertFiles.length === 1
          ? "Converting document to PDF..."
          : `Batch converting ${selectedConvertFiles.length} files to PDF...`;
      }
      btn.disabled = true;

      const formData = new FormData();
      selectedConvertFiles.forEach(file => {
        formData.append('files', file, file.name);
      });

      try {
        const res = await fetch('/api/convert', { method: 'POST', body: formData });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ message: 'Conversion failed' }));
          alert('Error: ' + (err.message || 'Server error during conversion'));
          progress.classList.add('hidden');
          btn.disabled = false;
          return;
        }

        const disposition = res.headers.get('Content-Disposition') || '';
        let filename = selectedConvertFiles.length > 1 ? 'converted_documents.zip' : (selectedConvertFiles[0].name.replace(/\\.[^/.]+$/, "") + ".pdf");
        const match = disposition.match(/filename="?([^";]+)"?/);
        if (match && match[1]) {
          filename = match[1];
        }

        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();
        closeConverterModal();
      } catch (err) {
        alert('Upload/Conversion error: ' + err);
        progress.classList.add('hidden');
        btn.disabled = false;
      }
    }

    fetchServices();
    fetchStats();
    fetchVPN();
    setInterval(fetchStats, 3000);
    setInterval(fetchServices, 4000);
  </script>

  <!-- Document to PDF Converter Modal -->
  <div id="converter-modal" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-lg hidden">
    <div class="golden-glass-card max-w-lg w-full p-6 sm:p-7 rounded-3xl space-y-4 border border-amber-500/40 shadow-2xl relative max-h-[90vh] flex flex-col justify-between">

      <!-- Modal Header -->
      <div class="flex items-start justify-between">
        <div class="flex items-center gap-3">
          <div class="p-2.5 rounded-2xl bg-amber-500/20 border border-amber-500/40 text-amber-300">
            <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path></svg>
          </div>
          <div>
            <h3 class="text-lg font-bold text-white">Batch Document to PDF</h3>
            <p class="text-xs text-amber-100/70">LibreOffice Batch Engine &bull; Zero Idle Memory</p>
          </div>
        </div>
        <button onclick="closeConverterModal()" class="text-amber-200/60 hover:text-white p-1 rounded-xl hover:bg-white/5 transition">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>

      <!-- Hidden Multi-file input -->
      <input type="file" id="convert-file-input" class="sr-only" accept="*/*" multiple>

      <!-- Dropzone (shown when 0 files selected) -->
      <label for="convert-file-input" id="convert-dropzone" class="border-2 border-dashed border-amber-500/40 hover:border-amber-400/80 bg-amber-500/5 hover:bg-amber-500/10 transition rounded-2xl p-6 text-center cursor-pointer flex flex-col items-center justify-center space-y-3 block">
        <svg class="w-10 h-10 text-amber-400/80 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"></path></svg>
        <div class="pointer-events-none">
          <p class="text-sm font-semibold text-white">Choose multiple files or drag & drop</p>
          <p class="text-xs text-amber-100/50 mt-1">Supports Word (.docx, .doc), PPT (.pptx), Excel (.xlsx), Images & Text</p>
        </div>
        <span class="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-amber-500/20 text-amber-300 border border-amber-500/40 text-xs font-semibold hover:bg-amber-500/30 transition pointer-events-none">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
          Select Files (Multi-select)
        </span>
      </label>

      <!-- Selected Files Container (shown when >= 1 files selected) -->
      <div id="convert-file-list-container" class="hidden space-y-3 flex-1 overflow-hidden flex flex-col">
        <div class="flex items-center justify-between text-xs pb-1.5 border-b border-amber-500/15">
          <span id="convert-file-summary" class="font-bold text-amber-300">0 files selected</span>
          <div class="flex items-center gap-2">
            <label for="convert-file-input" class="cursor-pointer text-[11px] px-2.5 py-1 rounded-lg bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 font-semibold border border-amber-500/30 transition flex items-center gap-1">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
              <span>+ Add More</span>
            </label>
            <button type="button" onclick="clearAllConvertFiles()" class="text-[11px] px-2.5 py-1 rounded-lg bg-white/5 text-amber-200/60 hover:text-white hover:bg-white/10 font-semibold transition">
              Clear All
            </button>
          </div>
        </div>
        <div id="convert-file-list" class="max-h-52 overflow-y-auto space-y-1.5 pr-1"></div>
      </div>

      <!-- Conversion Progress State -->
      <div id="convert-progress" class="hidden space-y-2">
        <div class="flex items-center justify-between text-xs">
          <span class="text-amber-300 font-medium flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
            <span id="convert-status-text">Converting documents to PDF...</span>
          </span>
          <span class="mono text-amber-400" id="convert-status-pct">Batch active</span>
        </div>
        <div class="w-full bg-black/40 h-1.5 rounded-full overflow-hidden border border-amber-500/20">
          <div class="h-full bg-gradient-to-r from-amber-500 to-amber-300 rounded-full animate-pulse w-full"></div>
        </div>
      </div>

      <!-- Action Buttons -->
      <div class="pt-2 flex items-center justify-end gap-3 border-t border-amber-500/15">
        <button onclick="closeConverterModal()" class="px-4 py-2 rounded-xl text-xs font-semibold text-amber-200/80 hover:bg-white/5 transition">Cancel</button>
        <button id="convert-btn" onclick="executeConversion()" disabled class="gold-gradient-btn px-5 py-2.5 rounded-xl text-xs font-bold disabled:opacity-40 disabled:cursor-not-allowed">
          Convert & Download PDF
        </button>
      </div>

    </div>
  </div>


</body>
</html>
"""



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
                return verify_session_token(c['session_token'].value)
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
                self.send_header("Set-Cookie", f"session_token={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))
                self.send_header("Set-Cookie", f"session_user={username}; Path=/; Max-Age=2592000; SameSite=Lax" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))
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

        elif self.path == "/api/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))
            self.send_header("Set-Cookie", "session_user=; Path=/; Max-Age=0; SameSite=Lax" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))
            body = b'{"status": "ok"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            target = '=agy:'
            try:
                mode = subprocess.check_output(['tmux', 'display-message', '-p', '-t', target, '#{pane_in_mode}'], timeout=2, stderr=subprocess.DEVNULL).decode().strip()
                if action in ('up', 'top'):
                    subprocess.run(['tmux', 'copy-mode', '-e', '-t', target], check=True, timeout=2, stderr=subprocess.DEVNULL)
                if action == 'bottom':
                    if mode == '1':
                        subprocess.run(['tmux', 'send-keys', '-t', target, '-X', 'cancel'], check=True, timeout=2)
                elif action in ('up', 'top') or mode == '1':
                    command = {'up':'scroll-up', 'down':'scroll-down', 'top':'history-top'}[action]
                    subprocess.run(['tmux', 'send-keys', '-t', target, '-X', '-N', str(lines), command], check=True, timeout=2)
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
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
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

        elif self.path == "/api/services":
            user = self.get_session_user()
            if not is_admin(user):
                self.send_response(403)
                self.end_headers()
                return
            data = get_services_report()
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
                body = ADMIN_DASHBOARD_HTML.encode("utf-8")
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
