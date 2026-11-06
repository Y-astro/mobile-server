"""Antra upstream HTTP and WebSocket client.

Handles authenticated API communication with Antra server,
session cookies, track downloads, acknowledgements, and WebSocket event streams.
Never logs credentials, session cookies, ws_tokens, or raw bodies.
Nothing starts on import.
"""
import http.cookiejar
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from typing import Any, Dict, Generator, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG_DIR = Path(os.path.expanduser("~/.config/antra"))
DEFAULT_CREDENTIALS_FILE = DEFAULT_CONFIG_DIR / "credentials.json"
DEFAULT_COOKIES_FILE = DEFAULT_CONFIG_DIR / "cookies.json"
DEFAULT_BASE_URL = "https://antra.hoshi.cfd"

SUPPORTED_MUSIC_HOSTS = (
    "spotify.com",
    "open.spotify.com",
    "deezer.com",
    "www.deezer.com",
    "tidal.com",
    "listen.tidal.com",
    "qobuz.com",
    "open.qobuz.com",
    "play.qobuz.com",
    "music.apple.com",
    "music.amazon.com",
    "amazon.com",
)


def is_supported_music_url(url: str) -> bool:
    """Validate that url is an HTTPS URL matching the supported music allowlist."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return False

    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False

    # Guard against IP addresses and localhost
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local") or host.endswith(".internal"):
        return False

    return any(host == allowed or host.endswith("." + allowed) for allowed in SUPPORTED_MUSIC_HOSTS)


class AntraClientError(Exception):
    """Base error for Antra client operations."""
    pass


class MissingCredentialsError(AntraClientError):
    """Raised when credentials.json is missing or incomplete."""
    pass


class MissingDependencyError(AntraClientError):
    """Raised when a declared external dependency (e.g. websockets) is absent."""
    pass


class ConcurrencyLimitError(AntraClientError):
    """Raised when upstream returns a 429 concurrency limit."""
    pass


class DailyLimitError(AntraClientError):
    """Raised when upstream returns a 429 daily limit."""
    pass


class UncertainSubmissionError(AntraClientError):
    """Raised when a job submission timed out or failed without a confirmed job_id."""
    pass


class AntraClient:
    """Outbound client for Antra upstream services."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        config_dir: Optional[Path] = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.config_dir = Path(config_dir) if config_dir is not None else DEFAULT_CONFIG_DIR
        self.credentials_file = self.config_dir / "credentials.json"
        self.cookies_file = self.config_dir / "cookies.json"
        self.timeout = timeout
        self.cookie_jar = http.cookiejar.CookieJar()
        self._load_cookies()

    def _ensure_config_dir(self) -> None:
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.config_dir, 0o700)
        except OSError:
            pass

    def has_credentials(self) -> bool:
        if not self.credentials_file.is_file():
            return False
        try:
            with open(self.credentials_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("username") and data.get("password"))
        except Exception:
            return False

    def load_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.has_credentials():
            return None, None
        try:
            with open(self.credentials_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("username"), data.get("password")
        except Exception:
            return None, None

    def _load_cookies(self) -> None:
        if not self.cookies_file.is_file():
            return
        try:
            with open(self.cookies_file, "r", encoding="utf-8") as f:
                cookie_dict = json.load(f)
            parsed_base = urllib.parse.urlparse(self.base_url)
            domain = parsed_base.hostname or ""
            for name, val in cookie_dict.items():
                ck = http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=val,
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=bool(domain),
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=parsed_base.scheme == "https",
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
                self.cookie_jar.set_cookie(ck)
        except Exception:
            pass

    def _save_cookies(self) -> None:
        self._ensure_config_dir()
        parsed_base = urllib.parse.urlparse(self.base_url)
        domain = parsed_base.hostname or ""
        out = {}
        for ck in self.cookie_jar:
            if not domain or ck.domain == domain or domain.endswith(ck.domain.lstrip(".")):
                out[ck.name] = ck.value
        try:
            with open(self.cookies_file, "w", encoding="utf-8") as f:
                json.dump(out, f)
            os.chmod(self.cookies_file, 0o600)
        except OSError:
            pass

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: List[Any] = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        if self.base_url.startswith("https://"):
            ctx = ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        req_headers = {"User-Agent": "Antra-Runtime/1.0"}
        if headers:
            req_headers.update(headers)

        body_bytes = None
        if data is not None:
            body_bytes = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
        opener = self._build_opener()

        try:
            resp = opener.open(req, timeout=self.timeout)
            self._save_cookies()
            if stream:
                return resp
            resp_body = resp.read()
            return resp.status, resp.headers, resp_body
        except urllib.error.HTTPError as e:
            self._save_cookies()
            err_body = e.read()
            error_code = e.headers.get("X-Antra-Error-Code", "")
            if e.code == 429:
                if error_code == "DAILY_LIMIT" or b"DAILY_LIMIT" in err_body or b"daily" in err_body.lower():
                    raise DailyLimitError("Daily track limit reached on Antra upstream")
                raise ConcurrencyLimitError("Concurrency limit reached on Antra upstream; backing off")
            raise AntraClientError(f"HTTP {e.code}: upstream request failed")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            raise AntraClientError(f"Network error communicating with Antra: {e}")

    def login(self) -> bool:
        """Authenticate with upstream Antra using configured credentials."""
        user, pwd = self.load_credentials()
        if not user or not pwd:
            raise MissingCredentialsError("No Antra credentials found in credentials.json")

        status, _, body = self._request("POST", "/api/auth/login", data={"username": user, "password": pwd})
        if status == 200:
            self._save_cookies()
            return True
        return False

    def get_me(self) -> Dict[str, Any]:
        """Fetch authenticated user profile details from /api/auth/me."""
        status, _, body = self._request("GET", "/api/auth/me")
        if status != 200:
            raise AntraClientError(f"Failed to fetch user info: HTTP {status}")
        return json.loads(body.decode("utf-8"))

    def resolve(self, music_url: str, format_pref: str = "flac24") -> Dict[str, Any]:
        """Resolve music URL into title, artist, track count, and track listings."""
        if not is_supported_music_url(music_url):
            raise AntraClientError("URL is not in the supported music allowlist")

        status, _, body = self._request("POST", "/api/resolve", data={"url": music_url, "format": format_pref})
        if status != 200:
            raise AntraClientError(f"Resolve failed: HTTP {status}")

        data = json.loads(body.decode("utf-8"))
        raw_tracks = data.get("tracks") or []
        parsed_tracks = []
        for i, trk in enumerate(raw_tracks):
            parsed_tracks.append({
                "index": i,
                "title": trk.get("title") or f"Track {i + 1}",
                "artist": trk.get("artist") or data.get("artist") or "",
                "status": "queued",
                "bit_depth": None,
                "sample_rate": None,
                "filename": None,
                "message": "",
            })

        return {
            "title": data.get("release_name") or (raw_tracks[0].get("title") if raw_tracks else "Unknown Release"),
            "artist": data.get("artist") or (raw_tracks[0].get("artist") if raw_tracks else ""),
            "release_type": data.get("release_type", "playlist"),
            "artwork_url": data.get("artwork_url", ""),
            "tracks": parsed_tracks,
            "total": len(parsed_tracks),
        }

    def submit_job(
        self,
        music_url: str,
        start_index: int,
        end_index: int,
        format_pref: str = "flac24",
    ) -> Tuple[str, str]:
        """Submit a download batch. Raises UncertainSubmissionError on ambiguous timeout."""
        payload = {
            "url": music_url,
            "format": format_pref,
            "start_index": start_index,
            "end_index": end_index,
            "client_packaging": True,
        }
        try:
            status, _, body = self._request("POST", "/api/jobs", data=payload)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            # Ambiguous timeout with no confirmed upstream job ID
            raise UncertainSubmissionError(
                f"Job submission timed out without confirmed job_id: {e}"
            )

        if status not in (200, 201, 202):
            raise AntraClientError(f"Job submission failed: HTTP {status}")

        resp_data = json.loads(body.decode("utf-8"))
        job_id = resp_data.get("job_id")
        ws_token = resp_data.get("ws_token") or ""
        if not job_id:
            raise UncertainSubmissionError("Submission response missing job_id")

        return str(job_id), str(ws_token)

    def download_track_stream(self, upstream_job_id: str, track_index: int) -> Any:
        """Stream audio data for a single track."""
        path = f"/api/jobs/{upstream_job_id}/tracks/{track_index}/download"
        return self._request("GET", path, stream=True)

    def ack_track(self, upstream_job_id: str, track_index: int) -> bool:
        """Acknowledge a durable local write to upstream. Never raises on failure."""
        path = f"/api/jobs/{upstream_job_id}/tracks/{track_index}/ack"
        try:
            status, _, _ = self._request("POST", path)
            return status in (200, 204)
        except Exception:
            return False

    def connect_job_ws(
        self,
        upstream_job_id: str,
        ws_token: str,
    ) -> Generator[Dict[str, Any], None, None]:
        """Connect to WebSocket event stream for job and yield events.

        Requires 'websockets' pure-Python / standard package.
        """
        try:
            import websockets.sync.client
        except ImportError:
            raise MissingDependencyError(
                "WebSocket dependency 'websockets' not installed. Provision via requirements-antra.txt."
            )

        ws_proto = "wss" if self.base_url.startswith("https") else "ws"
        host = urllib.parse.urlparse(self.base_url).netloc
        ws_url = f"{ws_proto}://{host}/api/jobs/{upstream_job_id}/ws?ws_token={urllib.parse.quote(ws_token)}"

        # Forward session cookies if available
        headers = {}
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in self.cookie_jar)
        if cookie_header:
            headers["Cookie"] = cookie_header

        with websockets.sync.client.connect(ws_url, additional_headers=headers) as ws:
            for message in ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                try:
                    data = json.loads(message)
                    yield data
                    if data.get("type") == "complete":
                        break
                except Exception:
                    continue
