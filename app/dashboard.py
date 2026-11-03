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
from notes import NotesStore
from document_conversion import convert_documents_batch
from auth import CredentialStore, LoginLimiter
from telemetry import Telemetry
from limits import Admission, BODY_LIMITS, CONVERSION_PATHS, canonical_post_path
from tor_proxy import TorProxy
from laptop import LaptopClient, LaptopError
from service_catalog import dashboard_services, suwayomi_probe_ports
from role_templates import read_role_template, RoleTemplateUnavailable
from agy_sessions import AgySessions
AGY_SESSIONS = AgySessions(prefix=PREFIX, tmux_bin=TMUX_BIN)
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
NOTES_STORE = NotesStore(NOTES_FILE, lock=NOTES_LOCK)

def get_user_notes(username):
    return NOTES_STORE.get(username)

def set_user_notes(username, content):
    return NOTES_STORE.set(username, content)

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
SERVICES_CONFIG = dashboard_services()

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
    all_active_complete = True
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
                "can_toggle": False,
                "memory_complete": True
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
                check_ports.extend(suwayomi_probe_ports())
            for cp in check_ports:
                if is_service_port_open(cp):
                    port_listening = True
                    break

        running = (len(pids) > 0) or port_listening or (not is_disabled and is_healthy)

        rss_mb = 0.0
        service_memory_complete = True
        if running:
            if pids:
                rss_values = [get_process_rss(pid) for pid in pids]
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
            "memory_complete": service_memory_complete,
            "unread_updates": unread_updates,
            "unread_titles": unread_titles,
            "latest_unread": latest_unread
        })

    tot_mem = round(total_active_mem, 1) if all_active_complete else None
    return {
        "services": services,
        "active_memory_mb": tot_mem,
        "total_active_mem_mb": tot_mem,
        "memory_complete": all_active_complete,
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


# AGY MULTI-INSTANCE SESSION MANAGER
# =====================================================================
def get_tmux_cmd():
    return AGY_SESSIONS.get_tmux_cmd()

def list_agy_sessions():
    return AGY_SESSIONS.list_agy_sessions()

def create_agy_session(ident=None):
    return AGY_SESSIONS.create_agy_session(ident=ident)

def close_agy_session(ident):
    return AGY_SESSIONS.close_agy_session(ident)


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
        post_path = canonical_post_path(self.path)
        try:
            if self.headers.get("Transfer-Encoding"):
                self.json_error(400, "Use a Content-Length request")
                return
            value = self.headers.get("Content-Length", "0")
            if not value.isdecimal():
                self.json_error(400, "Invalid Content-Length")
                return
            length = int(value)
            if length > BODY_LIMITS.get(post_path, 0):
                self.json_error(413, "Request exceeds this endpoint's size limit")
                return
            slot, acquired = ADMISSION.acquire(post_path)
            if not acquired:
                self.json_error(429, "This operation is busy; retry shortly", retry_after=3)
                return
            self._do_POST(post_path=post_path)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            self.json_error(400, "Invalid request body")
        except (TimeoutError, socket.timeout):
            self.json_error(408, "Upload stalled; retry the request")
        finally:
            if slot is not None and acquired:
                slot.release()

    def _do_POST(self, post_path=None):
        route = post_path if post_path is not None else canonical_post_path(self.path)
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

        elif route == "/api/convert":
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
            try:
                body = read_role_template(STATIC_DIR, "login")
            except RoleTemplateUnavailable:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                msg = b"Service Unavailable: Login page unavailable"
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
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
            self.send_header("Set-Cookie", "session_token_http=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
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
            role = "admin" if is_admin(user) else "family"
            try:
                body = read_role_template(STATIC_DIR, role)
            except RoleTemplateUnavailable:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                msg = b"Service Unavailable: Dashboard page unavailable"
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
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
