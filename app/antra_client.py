"""Antra upstream HTTP and WebSocket client.

Handles authenticated API communication with Antra server,
MozillaCookieJar cookies.txt persistence, expired-session recovery,
track downloads, acknowledgements, and WebSocket event streams.
Never logs credentials, session cookies, ws_tokens, or raw bodies.
Nothing starts on import.
"""
import http.cookiejar
import json
import logging
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
import uuid

logger = logging.getLogger("antra_client")

DEFAULT_CONFIG_DIR = Path(os.path.expanduser("~/.config/antra"))
DEFAULT_CREDENTIALS_FILE = DEFAULT_CONFIG_DIR / "credentials.json"
DEFAULT_COOKIES_FILE = DEFAULT_CONFIG_DIR / "cookies.txt"
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

    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return False
    try:
        if parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False

    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local") or host.endswith(".internal"):
        return False

    return any(host == allowed or host.endswith("." + allowed) for allowed in SUPPORTED_MUSIC_HOSTS)


class AntraClientError(Exception):
    """Safe public error with optional retry scheduling and HTTP status."""
    def __init__(self, message, *, retry_after=None, status=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


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
        self.cookies_file = self.config_dir / "cookies.txt"
        self.timeout = timeout
        self.cookie_jar = http.cookiejar.MozillaCookieJar(filename=str(self.cookies_file))
        self._load_cookies()

    def _ensure_config_dir(self) -> None:
        if self.config_dir.is_symlink():
            raise MissingCredentialsError("Antra configuration must be private")
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.config_dir, 0o700)
        except OSError:
            pass

    def has_credentials(self) -> bool:
        if not self.credentials_file.is_file() or self.credentials_file.is_symlink():
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
        if self.cookies_file.is_file() and not self.cookies_file.is_symlink():
            try:
                self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass

    def _save_cookies(self) -> None:
        self._ensure_config_dir()
        if self.cookies_file.is_symlink():
            return
        tmp_file = self.cookies_file.parent / f".tmp_{uuid.uuid4().hex}"
        try:
            descriptor = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            self.cookie_jar.save(filename=str(tmp_file), ignore_discard=True, ignore_expires=True)
            with tmp_file.open("rb") as saved:
                os.fsync(saved.fileno())
            os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, self.cookies_file)
            os.chmod(self.cookies_file, 0o600)
        except OSError:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: List[Any] = [urllib.request.HTTPCookieProcessor(self.cookie_jar), NoRedirects()]
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
        is_submission: bool = False,
        _reauth_attempt: bool = False,
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
            with resp:
                resp_body = resp.read(2 * 1024 * 1024 + 1)
                if len(resp_body) > 2 * 1024 * 1024:
                    if is_submission:
                        raise UncertainSubmissionError("Submission response exceeded size limit")
                    raise AntraClientError("Upstream response exceeded size limit")
                return resp.status, resp.headers, resp_body
        except urllib.error.HTTPError as e:
            self._save_cookies()
            with e:
                err_body = e.read(64 * 1024)
                error_code = e.headers.get("X-Antra-Error-Code", "")

            # Expired session recovery: 401 on authenticated endpoint -> try login once
            if e.code == 401 and not _reauth_attempt and path != "/api/auth/login":
                if self.has_credentials():
                    try:
                        if self.login():
                            return self._request(
                                method, path, data=data, headers=headers,
                                stream=stream, is_submission=is_submission,
                                _reauth_attempt=True
                            )
                    except Exception:
                        pass

            if e.code == 429:
                raw_retry = e.headers.get("Retry-After", "")
                retry_after = float(raw_retry) if raw_retry.isdigit() else None
                if error_code == "DAILY_LIMIT" or b"DAILY_LIMIT" in err_body or b"daily" in err_body.lower():
                    raise DailyLimitError("Daily quota reached; downloads will resume after reset.", retry_after=retry_after or (86402 - time.time() % 86400))
                raise ConcurrencyLimitError("Provider is busy; waiting before retry.", retry_after=retry_after or 15)
            if is_submission and e.code >= 500:
                raise UncertainSubmissionError("Provider failed before confirming submission")
            raise AntraClientError(f"Upstream request failed (HTTP {e.code}).", status=e.code)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            if is_submission:
                raise UncertainSubmissionError(
                    "Job submission interrupted without a confirmed identifier."
                )
            raise AntraClientError("Network request failed; retry later.")

    def login(self) -> bool:
        """Authenticate with upstream Antra using configured credentials."""
        user, pwd = self.load_credentials()
        if not user or not pwd:
            raise MissingCredentialsError("No Antra credentials found in credentials.json")

        status, _, _ = self._request("POST", "/api/auth/login", data={"username": user, "password": pwd})
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
        raw_tracks = data.get("tracks") if isinstance(data, dict) else None
        if not isinstance(raw_tracks, list) or not raw_tracks or len(raw_tracks) > 10000 or any(not isinstance(track, dict) for track in raw_tracks):
            raise AntraClientError("Provider returned an invalid track list.", status=400)
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
        """Submit a download batch.

        Indices are zero-based integer start and exclusive integer end per app.js1276 / queue.js300.
        Raises UncertainSubmissionError on timeout or response without job_id.
        """
        if type(start_index) is not int or type(end_index) is not int or isinstance(start_index, bool) or isinstance(end_index, bool):
            raise ValueError("Indices must be integers, not bool")
        if start_index < 0 or end_index <= start_index:
            raise ValueError("Invalid index range")

        payload = {
            "url": music_url,
            "format": format_pref,
            "start_index": start_index,
            "end_index": end_index,
            "client_packaging": True,
        }

        status, _, body = self._request("POST", "/api/jobs", data=payload, is_submission=True)

        if status not in (200, 201, 202):
            raise AntraClientError(f"Job submission failed: HTTP {status}")

        try:
            resp_data = json.loads(body.decode("utf-8"))
        except Exception:
            raise UncertainSubmissionError("Submission response was malformed JSON")

        if not isinstance(resp_data, dict):
            raise UncertainSubmissionError("Submission response was not an object")
        job_id = resp_data.get("job_id")
        ws_token = resp_data.get("ws_token") or ""
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise UncertainSubmissionError("Submission response missing a valid job identifier")

        return job_id, ws_token if isinstance(ws_token, str) else ""

    def get_job_status(self, upstream_job_id: str) -> Dict[str, Any]:
        """Check status of known upstream job."""
        status, _, body = self._request("GET", f"/api/jobs/{upstream_job_id}/status")
        if status != 200:
            raise AntraClientError(f"Status check failed: HTTP {status}")
        return json.loads(body.decode("utf-8"))

    def fresh_ws_token(self, upstream_job_id: str) -> Optional[str]:
        """Request a fresh WebSocket token for an existing job (queue.js254)."""
        status, _, body = self._request("POST", f"/api/jobs/{upstream_job_id}/ws-token")
        data = json.loads(body.decode("utf-8"))
        token = data.get("ws_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise AntraClientError("Could not renew the existing download session.")
        return token

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
        """Connect to WebSocket event stream for job and yield events with bounded timeouts."""
        try:
            import websockets.sync.client
        except ImportError:
            raise MissingDependencyError(
                "WebSocket dependency 'websockets' not installed. Provision via requirements-antra.txt."
            )

        ws_proto = "wss" if self.base_url.startswith("https") else "ws"
        host = urllib.parse.urlparse(self.base_url).netloc
        ws_url = f"{ws_proto}://{host}/api/jobs/{upstream_job_id}/ws?ws_token={urllib.parse.quote(ws_token)}"

        headers = {}
        cookie_request = urllib.request.Request(self.base_url + "/api/jobs/")
        self.cookie_jar.add_cookie_header(cookie_request)
        cookie_header = cookie_request.get_header("Cookie")
        if cookie_header:
            headers["Cookie"] = cookie_header

        with websockets.sync.client.connect(
            ws_url,
            additional_headers=headers,
            open_timeout=15,
            close_timeout=10,
        ) as ws:
            while True:
                try:
                    message = ws.recv(timeout=30)
                except TimeoutError:
                    # Provider lookup may be quiet; WebSocket keepalive detects dead peers.
                    yield {"type": "heartbeat"}
                    continue
                except Exception as error:
                    raise AntraClientError("Download event stream interrupted; reconnecting.") from error

                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                try:
                    data = json.loads(message)
                    yield data
                    if data.get("type") == "complete":
                        break
                except Exception:
                    continue
