"""HTTP request dispatcher for Antra administration endpoints.

Enforces administrator-only authentication, full effective same-origin POST validation,
bounded body inputs, HEAD semantics, and strict Cache-Control: private, no-store.
Nothing starts on import.
"""
import io
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Optional
import urllib.parse

from antra_store import (
    AntraStore,
    InvalidTransitionError,
    JobNotFoundError,
    LOGICAL_OUTPUT_DIRECTORIES,
    VALID_DESTINATIONS,
)
from antra_client import AntraClient, is_supported_music_url

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_REQUEST_BYTES = 64 * 1024  # 64 KiB
STATIC_DIR = os.path.expanduser("~/.config/dashboard/static")


def _send_response(
    handler: Any,
    status_code: int,
    payload: Optional[Dict[str, Any]] = None,
    raw_body: Optional[bytes] = None,
    content_type: str = "application/json",
) -> None:
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    elif raw_body is not None:
        body = raw_body
    else:
        body = b""

    handler.send_response(status_code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "private, no-store")
    handler.end_headers()

    cmd = getattr(handler, "command", "GET")
    if cmd != "HEAD" and body:
        handler.wfile.write(body)


def _send_error(handler: Any, status_code: int, message: str) -> None:
    _send_response(handler, status_code, {"status": "error", "message": message})


def _is_full_same_origin(handler: Any) -> bool:
    """Validate full effective origin (scheme + host + port) for state-modifying requests."""
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return False

    host_header = handler.headers.get("Host", "").strip().lower()
    if not host_header:
        return False

    # Trust X-Forwarded-Proto only if specifically https, else fall back to http
    fwd_proto = handler.headers.get("X-Forwarded-Proto", "").strip().lower()
    expected_scheme = "https" if fwd_proto == "https" else "http"

    origin = handler.headers.get("Origin", "").strip()
    if origin:
        try:
            parsed = urllib.parse.urlsplit(origin)
            origin_scheme = parsed.scheme.lower()
            origin_netloc = parsed.netloc.lower()
            if origin_scheme != expected_scheme:
                return False
            if origin_netloc != host_header:
                return False
        except Exception:
            return False

    referer = handler.headers.get("Referer", "").strip()
    if referer and not origin:
        try:
            parsed_ref = urllib.parse.urlsplit(referer)
            ref_scheme = parsed_ref.scheme.lower()
            ref_netloc = parsed_ref.netloc.lower()
            if ref_scheme != expected_scheme:
                return False
            if ref_netloc != host_header:
                return False
        except Exception:
            return False

    return True


def dispatch_antra(
    handler: Any,
    user: Optional[str] = None,
    store: Optional[AntraStore] = None,
    client: Optional[AntraClient] = None,
    is_admin: Optional[Any] = None,
) -> None:
    """Route and dispatch Antra API and page requests."""
    method = getattr(handler, "command", "GET")
    if method not in ("GET", "HEAD", "POST"):
        _send_error(handler, 405, "Method not allowed")
        return

    # 1. Authentication check
    if user is None:
        get_user_fn = getattr(handler, "get_session_user", None)
        user = get_user_fn() if callable(get_user_fn) else None

    if not user:
        _send_error(handler, 401, "Authentication required")
        return

    # 2. Authorization check (admin only)
    admin_ok = False
    if callable(is_admin):
        admin_ok = is_admin(user)
    elif hasattr(handler, "is_admin") and callable(handler.is_admin):
        admin_ok = handler.is_admin(user)

    if not admin_ok:
        _send_error(handler, 403, "Administrator access required")
        return

    # 3. Same-origin protection for POST
    if method == "POST":
        if not _is_full_same_origin(handler):
            _send_error(handler, 403, "Cross-origin request forbidden")
            return

    raw_path = handler.path.split("?", 1)[0]
    ant_store = store or AntraStore()

    # Route: GET /antra or /antra/ (page view)
    if raw_path in ("/antra", "/antra/"):
        template_candidates = [
            Path(STATIC_DIR) / "antra.html",
            Path(__file__).resolve().parents[1] / "web" / "antra.html",
        ]
        html_bytes = None
        for cand in template_candidates:
            if cand.is_file() and not cand.is_symlink():
                try:
                    with open(cand, "rb") as f:
                        html_bytes = f.read()
                    break
                except OSError:
                    pass
        if html_bytes is not None:
            _send_response(handler, 200, raw_body=html_bytes, content_type="text/html; charset=utf-8")
        else:
            _send_response(
                handler,
                503,
                raw_body=b"Service Unavailable: Antra page template unavailable",
                content_type="text/plain; charset=utf-8",
            )
        return

    # Route: GET/HEAD /api/antra/jobs
    if raw_path in ("/api/antra/jobs", "/api/antra/jobs/"):
        if method in ("GET", "HEAD"):
            jobs = ant_store.list_jobs()
            worker_stat = ant_store.get_worker_status()
            settings = {
                "output_directories": dict(LOGICAL_OUTPUT_DIRECTORIES),
                "output_directory": LOGICAL_OUTPUT_DIRECTORIES["private"],
                "quality": "Best Available FLAC",
            }
            _send_response(handler, 200, {
                "jobs": jobs,
                "worker": worker_stat,
                "settings": settings,
            })
            return

        if method == "POST":
            content_length_str = handler.headers.get("Content-Length", "")
            if not content_length_str.isdigit() or len(content_length_str) > 12:
                _send_error(handler, 400, "Invalid Content-Length")
                return

            length = int(content_length_str)
            if length > MAX_REQUEST_BYTES:
                _send_error(handler, 413, "Request payload exceeds size limit")
                return

            try:
                body_raw = handler.rfile.read(length)
                payload = json.loads(body_raw.decode("utf-8")) if body_raw else {}
            except Exception:
                _send_error(handler, 400, "Malformed JSON request body")
                return

            if not isinstance(payload, dict):
                _send_error(handler, 400, "JSON payload must be an object")
                return

            url = payload.get("url")
            if not isinstance(url, str) or not url.strip():
                _send_error(handler, 400, "Missing or empty 'url' field")
                return

            if not is_supported_music_url(url):
                _send_error(handler, 400, "Unsupported or unallowed music URL")
                return

            destination = payload.get("destination", "private")
            if not isinstance(destination, str) or destination not in VALID_DESTINATIONS:
                _send_error(handler, 400, "Invalid destination: must be 'private' or 'family'")
                return

            confirm_dup = payload.get("confirm_duplicate", False)
            if "confirm_duplicate" in payload and not isinstance(confirm_dup, bool):
                _send_error(handler, 400, "Field 'confirm_duplicate' must be a boolean")
                return
            confirm_dup = bool(confirm_dup)
            existing_dup = ant_store.find_duplicate_job(url.strip(), destination=destination)
            if existing_dup and not confirm_dup:
                _send_response(handler, 409, {
                    "status": "warning",
                    "warning": "duplicate",
                    "message": f"This music URL is already queued or downloaded in {destination} music. Confirm to proceed anyway.",
                    "existing_job_id": existing_dup["id"],
                })
                return

            try:
                job = ant_store.create_job(url=url.strip(), destination=destination)
                if existing_dup:
                    ant_store.update_job(job["id"], message=f"Duplicate of {existing_dup['id'][:8]}. Confirmed by user.")
                    job = ant_store.get_job(job["id"])
                _send_response(handler, 202, {"job": job, "warning": "duplicate" if existing_dup else None})
            except Exception as e:
                _send_error(handler, 500, "Failed to enqueue job")
            return

    # Sub-routes for /api/antra/jobs/<id>[/cancel|/retry]
    if raw_path.startswith("/api/antra/jobs/"):
        remainder = raw_path[len("/api/antra/jobs/"):].strip("/")
        parts = remainder.split("/")

        job_id = parts[0]
        if not UUID_PATTERN.match(job_id):
            _send_error(handler, 400, "Invalid job identifier format")
            return

        if len(parts) == 1:
            if method in ("GET", "HEAD"):
                job = ant_store.get_job(job_id)
                if not job:
                    _send_error(handler, 404, "Job not found")
                    return
                tracks = ant_store.get_tracks(job_id)
                _send_response(handler, 200, {"job": job, "tracks": tracks})
                return
            _send_error(handler, 405, "Method not allowed")
            return

        if len(parts) == 2:
            action = parts[1]
            if method != "POST":
                _send_error(handler, 405, "Method not allowed")
                return

            if action == "cancel":
                try:
                    job = ant_store.cancel_job(job_id)
                    _send_response(handler, 202, {"job": job})
                except JobNotFoundError:
                    _send_error(handler, 404, "Job not found")
                except InvalidTransitionError as e:
                    _send_error(handler, 409, str(e))
                except Exception:
                    _send_error(handler, 500, "Cancel operation failed")
                return

            if action == "retry":
                try:
                    job = ant_store.retry_job(job_id)
                    _send_response(handler, 202, {"job": job})
                except JobNotFoundError:
                    _send_error(handler, 404, "Job not found")
                except InvalidTransitionError as e:
                    _send_error(handler, 409, str(e))
                except Exception:
                    _send_error(handler, 500, "Retry operation failed")
                return

            if action == "continue":
                try:
                    job = ant_store.continue_job(job_id)
                    _send_response(handler, 202, {"job": job})
                except JobNotFoundError:
                    _send_error(handler, 404, "Job not found")
                except InvalidTransitionError as e:
                    _send_error(handler, 409, str(e))
                except Exception:
                    _send_error(handler, 500, "Continue operation failed")
                return

            if action == "remove":
                try:
                    res = ant_store.remove_job(job_id)
                    _send_response(handler, 200, res)
                except JobNotFoundError:
                    _send_error(handler, 404, "Job not found")
                except InvalidTransitionError as e:
                    _send_error(handler, 409, str(e))
                except Exception:
                    _send_error(handler, 500, "Remove operation failed")
                return

            _send_error(handler, 404, "Unknown job action")
            return

    _send_error(handler, 404, "Unknown Antra endpoint")
