#!/data/data/com.termux/files/usr/bin/python3
"""
Suwayomi GraphQL Transparent Filter
Filters GraphQL queries for clients like Tsumiru to:
1. Restrict update feeds to the "Reading" category (id: 1).
2. Ignore all read chapters.
All other queries and data are passed through untouched.
"""

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

HOME = Path("/data/data/com.termux/files/home")
LOG_DIR = HOME / "suwayomi/data/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("suwayomi_filter")
handler = RotatingFileHandler(LOG_DIR / "graphql_filter.log", maxBytes=1024 * 1024, backupCount=2)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

reading_ids = []
last_refresh = 0
lock = threading.Lock()


def refresh_reading_ids():
    global reading_ids, last_refresh
    try:
        req = urllib.request.Request(
            f"{UPSTREAM}/suwayomi/api/graphql",
            data=json.dumps({"query": "query { category(id: 1) { mangas { nodes { id } } } }"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            ids = [m["id"] for m in data.get("data", {}).get("category", {}).get("mangas", {}).get("nodes", [])]
            with lock:
                reading_ids = ids
                last_refresh = time.time()
            return ids
    except Exception as e:
        logger.debug(f"Failed to refresh reading IDs: {e}")
        with lock:
            return list(reading_ids)


def get_reading_ids():
    with lock:
        ids = list(reading_ids)
        age = time.time() - last_refresh
    if not ids or age > CACHE_TTL:
        return refresh_reading_ids()
    return ids


def background_refresher():
    """Periodically refresh reading IDs so client requests never block."""
    while True:
        try:
            refresh_reading_ids()
        except Exception:
            pass
        time.sleep(CACHE_TTL)


class FilterHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Quiet logging for performance

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        
        # Pass GET to upstream
        try:
            req = urllib.request.Request(f"{UPSTREAM}{self.path}", method="GET")
            for k, v in self.headers.items():
                if k.lower() not in ("host",):
                    req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            self.send_response(502)
            self.end_headers()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""

        # Filter updates queries (Tsumiru uses GetChapterWithMangaPage)
        if b"GetChapterWithMangaPage" in body or (b"chapters" in body and b"FETCHED_AT" in body):
            try:
                data = json.loads(body.decode("utf-8"))
                current_reading_ids = get_reading_ids()
                if current_reading_ids:
                    variables = data.setdefault("variables", {})
                    flt = variables.setdefault("filter", {})
                    flt["inLibrary"] = {"equalTo": True}
                    flt["isRead"] = {"equalTo": False}
                    flt["mangaId"] = {"in": current_reading_ids}
                    body = json.dumps(data).encode("utf-8")
            except Exception as e:
                logger.error(f"Error filtering updates query: {e}")

        # Forward request to Suwayomi upstream
        try:
            req = urllib.request.Request(
                f"{UPSTREAM}{self.path}",
                data=body,
                method="POST"
            )
            for k, v in self.headers.items():
                if k.lower() not in ("host", "content-length"):
                    req.add_header(k, v)
            req.add_header("Content-Length", str(len(body)))

            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ("transfer-encoding", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            self.send_response(502)
            msg = f'{{"errors": [{{"message": "{str(e)}"}}]}}'.encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)


def run():
    # Start background refresh thread
    t = threading.Thread(target=background_refresher, daemon=True)
    t.start()

    server = http.server.ThreadingHTTPServer((BIND_HOST, BIND_PORT), FilterHandler)
    logger.info(f"Suwayomi GraphQL filter started on {BIND_HOST}:{BIND_PORT} -> {UPSTREAM}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
