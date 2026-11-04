#!/data/data/com.termux/files/usr/bin/python3
"""
Suwayomi GraphQL Transparent Filter
Filters GraphQL queries for clients like Tsumiru to:
1. Restrict update feeds to the "Reading" category (id: 1).
2. Ignore all read chapters.
All other queries and data are passed through untouched.
"""

import http.client
import http.server
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

UPSTREAM = "http://127.0.0.1:4569"
BIND_HOST = "127.0.0.1"
BIND_PORT = 4566
CACHE_TTL = 30  # seconds
FAILED_RETRY_COOLDOWN = 5.0  # seconds
CATEGORY_ID = 1
TIMEOUT_CATEGORY = 5
TIMEOUT_GET = 15
TIMEOUT_POST = 30

HOME = Path("/data/data/com.termux/files/home")
LOG_DIR = HOME / "suwayomi/data/logs"

# Hop-by-hop headers defined in RFC 2616 / RFC 7230 / RFC 9110
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Headers stripped when forwarding upstream response to client
STRIPPED_RESPONSE_HEADERS = HOP_BY_HOP_HEADERS | {"content-length", "server", "date"}

logger = logging.getLogger("suwayomi_filter")
logger.addHandler(logging.NullHandler())


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "graphql_filter.log", maxBytes=1024 * 1024, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class CacheColdError(Exception):
    """Raised when reading category cache has no successful snapshot and cannot refresh."""
    pass


class FilterPayloadError(Exception):
    """Raised when recognized GraphQL query has malformed JSON or invalid variables/filter shape."""
    pass


def parse_category_response(raw_bytes: bytes) -> list:
    """Parse GraphQL response for category mangas, returning list of integer/string IDs."""
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        raise ValueError("Malformed JSON from upstream") from e

    if not isinstance(payload, dict):
        raise ValueError("Upstream response is not a JSON object")

    if payload.get("errors"):
        raise ValueError("Upstream returned GraphQL errors")

    if "data" not in payload:
        raise ValueError("Missing 'data' field in upstream response")

    data = payload["data"]
    if not isinstance(data, dict):
        raise ValueError("'data' field is not a JSON object")

    if "category" not in data or data["category"] is None:
        # Valid category null or missing within otherwise valid GraphQL data means known empty
        return []

    category = data["category"]
    if not isinstance(category, dict):
        raise ValueError("'category' field is not a JSON object or null")

    if "mangas" not in category or category["mangas"] is None:
        raise ValueError("Missing or null 'mangas' field in category")

    mangas = category["mangas"]
    if not isinstance(mangas, dict):
        raise ValueError("'mangas' field is not a JSON object")

    if "nodes" not in mangas or mangas["nodes"] is None:
        raise ValueError("Missing or null 'nodes' field in mangas")

    nodes = mangas["nodes"]
    if not isinstance(nodes, list):
        raise ValueError("'nodes' field is not a JSON list")

    result = []
    for node in nodes:
        if not isinstance(node, dict) or "id" not in node:
            raise ValueError("Invalid node shape in nodes list")
        node_id = node["id"]
        if isinstance(node_id, bool) or not isinstance(node_id, (int, str)):
            raise ValueError("Invalid ID shape in nodes list")
        result.append(node_id)
    return result


class ReadingIdCache:
    """Single-flight coalesced cache owner for Suwayomi Reading category IDs."""

    def __init__(
        self,
        upstream: str = UPSTREAM,
        category_id: int = CATEGORY_ID,
        ttl: float = CACHE_TTL,
        retry_cooldown: float = FAILED_RETRY_COOLDOWN,
        timeout: float = TIMEOUT_CATEGORY,
    ):
        self.upstream = upstream
        self.category_id = category_id
        self.ttl = ttl
        self.retry_cooldown = retry_cooldown
        self.timeout = timeout
        self._lock = threading.Lock()
        self._snapshot = None
        self._state = "unknown"  # "unknown" or "ready"
        self._last_success = None
        self._last_failure = None
        self._refresh_in_flight = False
        self._refresh_event = None

    def get_reading_ids(self) -> list:
        """Return a copied snapshot of reading IDs, refreshing if expired."""
        now = time.monotonic()
        with self._lock:
            if self._state == "ready" and self._last_success is not None and (now - self._last_success) < self.ttl:
                return list(self._snapshot)
        return self._do_refresh(force=False)

    def refresh(self) -> list:
        """Force a refresh attempt (coalesced with in-flight refreshes)."""
        return self._do_refresh(force=True)

    def _do_refresh(self, force: bool = False) -> list:
        while True:
            now = time.monotonic()
            with self._lock:
                # Under-lock double check: if ordinary get (not forced), check if another thread refreshed
                if not force and self._state == "ready" and self._last_success is not None and (now - self._last_success) < self.ttl:
                    return list(self._snapshot)

                # If recently failed, honor retry cooldown to avoid hammering upstream
                if self._last_failure is not None and (now - self._last_failure) < self.retry_cooldown:
                    if self._state == "ready":
                        return list(self._snapshot)
                    raise CacheColdError("Reading category cache unavailable")

                if self._refresh_in_flight:
                    event = self._refresh_event
                else:
                    self._refresh_in_flight = True
                    self._refresh_event = threading.Event()
                    event = None

            if event is not None:
                # Wait for leader's in-flight refresh to finish
                event.wait(timeout=self.timeout + 1.0)
                with self._lock:
                    if self._state == "ready":
                        return list(self._snapshot)
                    raise CacheColdError("Reading category cache unavailable")

            # We are the leader thread doing the fetch
            try:
                ids = self._fetch_upstream()
                with self._lock:
                    self._snapshot = list(ids)
                    self._state = "ready"
                    self._last_success = time.monotonic()
                    self._refresh_in_flight = False
                    self._refresh_event.set()
                return list(ids)
            except Exception as e:
                with self._lock:
                    self._last_failure = time.monotonic()
                    self._refresh_in_flight = False
                    self._refresh_event.set()
                    if self._state == "ready":
                        logger.debug(f"Failed to refresh reading IDs, using stale snapshot: {e}")
                        return list(self._snapshot)
                    logger.debug(f"Failed cold refresh of reading IDs: {e}")
                    raise CacheColdError("Reading category cache unavailable") from e

    def _fetch_upstream(self) -> list:
        req = urllib.request.Request(
            f"{self.upstream}/suwayomi/api/graphql",
            data=json.dumps({"query": f"query {{ category(id: {self.category_id}) {{ mangas {{ nodes {{ id }} }} }} }}"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                return parse_category_response(body)
        except urllib.error.HTTPError as e:
            try:
                e.read()
            finally:
                e.close()
            raise


cache = ReadingIdCache()


def refresh_reading_ids():
    return cache.refresh()


def get_reading_ids():
    return cache.get_reading_ids()


def background_refresher(stop_event=None):
    """Periodically refresh reading IDs so client requests never block."""
    while stop_event is None or not stop_event.is_set():
        try:
            cache.refresh()
        except Exception:
            pass
        if stop_event is not None:
            if stop_event.wait(CACHE_TTL):
                break
        else:
            time.sleep(CACHE_TTL)


def is_recognized_updates_query(body: bytes) -> bool:
    return b"GetChapterWithMangaPage" in body or (b"chapters" in body and b"FETCHED_AT" in body)


def prepare_recognized_query(body: bytes) -> tuple[dict, dict, dict]:
    """
    Validate recognized query JSON and variables/filter structure before cache lookup.
    Returns (data, variables, flt).
    Raises FilterPayloadError on invalid JSON or malformed variables/filter.
    """
    try:
        decoded = body.decode("utf-8")
        data = json.loads(decoded)
    except Exception as e:
        raise FilterPayloadError(f"Invalid JSON in recognized query: {e}") from e

    if not isinstance(data, dict):
        raise FilterPayloadError("Recognized GraphQL request body must be a JSON object")

    variables = data.get("variables")
    if variables is None:
        variables = {}
        data["variables"] = variables
    elif not isinstance(variables, dict):
        raise FilterPayloadError("Variables must be a JSON object")

    flt = variables.get("filter")
    if flt is None:
        flt = {}
        variables["filter"] = flt
    elif not isinstance(flt, dict):
        raise FilterPayloadError("Filter must be a JSON object")

    return data, variables, flt


def apply_reading_constraints(data: dict, flt: dict, reading_ids: list) -> bytes:
    """Apply Reading category constraints to filter and return encoded bytes."""
    flt["inLibrary"] = {"equalTo": True}
    flt["isRead"] = {"equalTo": False}
    if reading_ids:
        flt["mangaId"] = {"in": list(reading_ids)}
    else:
        # Known empty reading category: contradictory IntFilterInput guarantees zero results
        flt["mangaId"] = {"equalTo": 0, "notEqualTo": 0}

    return json.dumps(data).encode("utf-8")


def transform_recognized_query(body: bytes, reading_ids: list) -> bytes:
    data, variables, flt = prepare_recognized_query(body)
    return apply_reading_constraints(data, flt, reading_ids)


def clean_request_headers(headers) -> dict:
    conn_val = headers.get("connection", "")
    connection_nominated = {
        name.strip().lower() for name in conn_val.split(",") if name.strip()
    }
    clean = {}
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower in ("host", "content-length"):
            continue
        if k_lower in HOP_BY_HOP_HEADERS or k_lower in connection_nominated:
            continue
        clean[k] = v
    return clean


def clean_response_headers(headers) -> list:
    conn_nominated = set()
    for k, v in headers:
        if k.lower() == "connection":
            for part in v.split(","):
                part_clean = part.strip().lower()
                if part_clean:
                    conn_nominated.add(part_clean)

    cleaned = []
    for k, v in headers:
        k_lower = k.lower()
        if k_lower in STRIPPED_RESPONSE_HEADERS or k_lower in conn_nominated:
            continue
        cleaned.append((k, v))
    return cleaned


class FilterHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    upstream = UPSTREAM
    cache = cache

    def log_message(self, format, *args):
        pass  # Quiet logging for performance

    def send_error_json(self, status: int, message: str):
        payload = json.dumps({"errors": [{"message": message}]}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _get_upstream(self) -> str:
        return getattr(self.server, "upstream", self.upstream)

    def _get_cache(self) -> ReadingIdCache:
        return getattr(self.server, "cache", self.cache)

    def _forward(self, method: str, body: bytes, timeout: float):
        upstream_url = self._get_upstream()
        clean_headers = clean_request_headers(self.headers)
        try:
            req = urllib.request.Request(
                f"{upstream_url}{self.path}",
                data=body if method == "POST" else None,
                method=method,
            )
            for k, v in clean_headers.items():
                req.add_header(k, v)
            if method == "POST":
                req.add_header("Content-Length", str(len(body)))

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in clean_response_headers(resp.getheaders()):
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read()
            finally:
                e.close()
            self.send_response(e.code)
            for k, v in clean_response_headers(e.headers.items()):
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception:
            self.send_error_json(502, "Bad Gateway")

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        # Pass GET to upstream
        self._forward("GET", b"", TIMEOUT_GET)

    def do_POST(self):
        try:
            content_len = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self.send_error_json(400, "Invalid Content-Length")
            return

        body = self.rfile.read(content_len) if content_len > 0 else b""

        # Filter updates queries (Tsumiru uses GetChapterWithMangaPage)
        if is_recognized_updates_query(body):
            # Validate recognized payload shape BEFORE category lookup
            try:
                data, variables, flt = prepare_recognized_query(body)
            except FilterPayloadError:
                self.send_error_json(400, "Invalid GraphQL request payload")
                return
            except Exception as e:
                logger.error(f"Error validating updates query: {e}")
                self.send_error_json(400, "Invalid GraphQL request payload")
                return

            cache_inst = self._get_cache()
            try:
                current_reading_ids = cache_inst.get_reading_ids()
            except CacheColdError:
                self.send_error_json(503, "Reading category cache unavailable")
                return
            except Exception as e:
                logger.error(f"Unexpected error getting reading IDs: {e}")
                self.send_error_json(503, "Reading category cache unavailable")
                return

            body = apply_reading_constraints(data, flt, current_reading_ids)

        # Forward request to Suwayomi upstream
        self._forward("POST", body, TIMEOUT_POST)


def run():
    setup_logging()

    # Start background refresh thread
    stop_event = threading.Event()
    t = threading.Thread(target=background_refresher, args=(stop_event,), daemon=True)
    t.start()

    server = http.server.ThreadingHTTPServer((BIND_HOST, BIND_PORT), FilterHandler)
    logger.info(f"Suwayomi GraphQL filter started on {BIND_HOST}:{BIND_PORT} -> {UPSTREAM}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    run()
