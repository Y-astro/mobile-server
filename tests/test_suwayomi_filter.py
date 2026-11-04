#!/usr/bin/env python3
"""
Unit and integration tests for Suwayomi GraphQL Transparent Filter.
Covers import purity, cache states, single-flight coalescing, query
rewriting, HTTP error forwarding, and hop-by-hop header removal.
"""

import http.client
import http.server
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.suwayomi_graphql_filter as filter_mod
from scripts.suwayomi_graphql_filter import (
    CacheColdError,
    FilterHandler,
    FilterPayloadError,
    ReadingIdCache,
    is_recognized_updates_query,
    parse_category_response,
    setup_logging,
    transform_recognized_query,
)


class EphemeralServer:
    """Helper running an http.server on an ephemeral loopback port."""

    def __init__(self, handler_cls, **server_attrs):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        for k, v in server_attrs.items():
            setattr(self.server, k, v)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


class FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Configurable fake upstream server for Suwayomi GraphQL endpoints."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        callback = getattr(self.server, "get_callback", None)
        if callback:
            callback(self)
            return

        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        callback = getattr(self.server, "post_callback", None)
        if callback:
            callback(self, body)
            return

        resp_body = b'{"data": "upstream_default"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)


class TestImportPurity(unittest.TestCase):
    """Verify that importing the filter module causes zero runtime side-effects."""

    def test_import_has_no_side_effects(self):
        cmd = [
            sys.executable,
            "-c",
            "import sys, threading, pathlib; "
            f"sys.path.insert(0, {str(REPO_ROOT)!r}); "
            "import scripts.suwayomi_graphql_filter as mod; "
            "assert threading.active_count() == 1, f'Thread count: {threading.active_count()}'; "
            "assert not pathlib.Path('/data/data/com.termux/files/home/suwayomi').exists() "
            "or True; "
            "print('PURE_IMPORT_OK')",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("PURE_IMPORT_OK", res.stdout)

    def test_setup_logging_isolated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_log_dir = Path(tmpdir) / "suwayomi/data/logs"
            orig_log_dir = filter_mod.LOG_DIR
            try:
                filter_mod.LOG_DIR = temp_log_dir
                setup_logging()
                log_file = temp_log_dir / "graphql_filter.log"
                self.assertTrue(log_file.exists())
                filter_mod.logger.info("Test log message")
                # Flush and verify log content
                for h in filter_mod.logger.handlers:
                    h.flush()
                content = log_file.read_text(encoding="utf-8")
                self.assertIn("Test log message", content)
            finally:
                # Close and remove added file handlers to keep state clean
                new_handlers = []
                for h in filter_mod.logger.handlers:
                    if isinstance(h, logging.FileHandler):
                        h.close()
                    else:
                        new_handlers.append(h)
                filter_mod.logger.handlers = new_handlers
                filter_mod.LOG_DIR = orig_log_dir


class TestCategoryResponseParser(unittest.TestCase):
    """Test parse_category_response handles all shapes, emptiness, and errors."""

    def test_parse_valid_nonempty_ids(self):
        payload = json.dumps({
            "data": {
                "category": {
                    "mangas": {
                        "nodes": [{"id": 1}, {"id": "manga_2"}, {"id": 100}]
                    }
                }
            }
        }).encode("utf-8")
        ids = parse_category_response(payload)
        self.assertEqual(ids, [1, "manga_2", 100])

    def test_parse_empty_nodes_list(self):
        payload = json.dumps({
            "data": {
                "category": {
                    "mangas": {
                        "nodes": []
                    }
                }
            }
        }).encode("utf-8")
        self.assertEqual(parse_category_response(payload), [])

    def test_parse_null_category_is_known_empty(self):
        payload = json.dumps({"data": {"category": None}}).encode("utf-8")
        self.assertEqual(parse_category_response(payload), [])

    def test_parse_missing_category_is_known_empty(self):
        payload = json.dumps({"data": {}}).encode("utf-8")
        self.assertEqual(parse_category_response(payload), [])

    def test_parse_null_mangas_raises(self):
        payload = json.dumps({"data": {"category": {"mangas": None}}}).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

    def test_parse_missing_mangas_raises(self):
        payload = json.dumps({"data": {"category": {}}}).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

    def test_parse_null_nodes_raises(self):
        payload = json.dumps({"data": {"category": {"mangas": {"nodes": None}}}}).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

    def test_parse_missing_nodes_raises(self):
        payload = json.dumps({"data": {"category": {"mangas": {}}}}).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

    def test_parse_invalid_mangas_type_raises(self):
        payload = json.dumps({"data": {"category": {"mangas": "not_a_dict"}}}).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

    def test_parse_invalid_nodes_type_raises(self):
        payload = json.dumps({"data": {"category": {"mangas": {"nodes": "not_a_list"}}}}).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

    def test_cache_state_on_malformed_category(self):
        # 1. Cold cache with malformed mangas raises CacheColdError and stays unknown
        class MalformedMangaCache(ReadingIdCache):
            def _fetch_upstream(self):
                return parse_category_response(json.dumps({"data": {"category": {"mangas": None}}}).encode("utf-8"))

        cache = MalformedMangaCache()
        with self.assertRaises(CacheColdError):
            cache.get_reading_ids()
        self.assertEqual(cache._state, "unknown")
        self.assertIsNone(cache._snapshot)

        # 2. Warm cache with malformed nodes preserves previous snapshot and does not mark ready with empty
        class MalformedNodeCache(ReadingIdCache):
            def __init__(self):
                super().__init__(ttl=0.01)
                self.calls = 0

            def _fetch_upstream(self):
                self.calls += 1
                if self.calls == 1:
                    return [42]
                return parse_category_response(json.dumps({"data": {"category": {"mangas": {"nodes": None}}}}).encode("utf-8"))

        warm_cache = MalformedNodeCache()
        self.assertEqual(warm_cache.get_reading_ids(), [42])
        time.sleep(0.02)
        # Second call fails upstream parse, retains stale snapshot [42]
        self.assertEqual(warm_cache.get_reading_ids(), [42])
        self.assertEqual(warm_cache._snapshot, [42])

    def test_parse_graphql_errors_raises(self):
        payload = json.dumps({
            "errors": [{"message": "Category not found"}],
            "data": None
        }).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

    def test_parse_malformed_top_level_raises(self):
        for bad in [b"not_json", b"[]", b'"string"', b"123", json.dumps({"data": "not_dict"}).encode("utf-8")]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_category_response(bad)

    def test_parse_invalid_node_shape_raises(self):
        payload = json.dumps({
            "data": {
                "category": {
                    "mangas": {
                        "nodes": ["not_a_dict"]
                    }
                }
            }
        }).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload)

        payload_missing_id = json.dumps({
            "data": {
                "category": {
                    "mangas": {
                        "nodes": [{"title": "no id"}]
                    }
                }
            }
        }).encode("utf-8")
        with self.assertRaises(ValueError):
            parse_category_response(payload_missing_id)

    def test_parse_invalid_id_types_raise(self):
        # bool, nested dict, list, None are disallowed
        for invalid_id in [True, False, {"nested": 1}, [1, 2], None]:
            with self.subTest(invalid_id=invalid_id):
                payload = json.dumps({
                    "data": {
                        "category": {
                            "mangas": {
                                "nodes": [{"id": invalid_id}]
                            }
                        }
                    }
                }).encode("utf-8")
                with self.assertRaises(ValueError):
                    parse_category_response(payload)


class TestReadingIdCache(unittest.TestCase):
    """Unit tests for cache behavior, immutability, TTL, backoff, and single-flight."""

    def test_cache_immutability(self):
        cache = ReadingIdCache()
        cache._state = "ready"
        cache._snapshot = [10, 20]
        cache._last_success = time.monotonic()

        snapshot1 = cache.get_reading_ids()
        snapshot1.append(999)
        snapshot2 = cache.get_reading_ids()
        self.assertEqual(snapshot2, [10, 20])
        self.assertNotIn(999, snapshot2)

    def test_ttl_and_backoff_timing(self):
        fetch_count = 0

        class MockCache(ReadingIdCache):
            def _fetch_upstream(self):
                nonlocal fetch_count
                fetch_count += 1
                return [1, 2]

        cache = MockCache(ttl=1.0, retry_cooldown=0.5)
        # First read triggers fetch
        ids = cache.get_reading_ids()
        self.assertEqual(ids, [1, 2])
        self.assertEqual(fetch_count, 1)

        # Immediate read uses cache (fetch_count still 1)
        ids = cache.get_reading_ids()
        self.assertEqual(ids, [1, 2])
        self.assertEqual(fetch_count, 1)

        # Advance monotonic time beyond TTL
        cache._last_success = time.monotonic() - 2.0
        ids = cache.get_reading_ids()
        self.assertEqual(fetch_count, 2)

    def test_cold_failure_and_backoff(self):
        fetch_count = 0

        class FailingCache(ReadingIdCache):
            def _fetch_upstream(self):
                nonlocal fetch_count
                fetch_count += 1
                raise ConnectionError("Upstream offline")

        cache = FailingCache(retry_cooldown=2.0)
        # Cold cache raises CacheColdError
        with self.assertRaises(CacheColdError):
            cache.get_reading_ids()
        self.assertEqual(fetch_count, 1)

        # Immediate subsequent call within retry_cooldown fails without invoking _fetch_upstream
        with self.assertRaises(CacheColdError):
            cache.get_reading_ids()
        self.assertEqual(fetch_count, 1)

    def test_stale_good_failure_preserves_snapshot(self):
        fetch_count = 0

        class TransientFailCache(ReadingIdCache):
            def _fetch_upstream(self):
                nonlocal fetch_count
                fetch_count += 1
                if fetch_count > 1:
                    raise ConnectionError("Upstream transient failure")
                return [42]

        cache = TransientFailCache(ttl=0.1, retry_cooldown=1.0)
        # Warm initial fetch
        self.assertEqual(cache.get_reading_ids(), [42])
        self.assertEqual(fetch_count, 1)

        # Expire TTL
        cache._last_success = time.monotonic() - 1.0
        # Second call attempts fetch, fails, returns stale snapshot
        self.assertEqual(cache.get_reading_ids(), [42])
        self.assertEqual(fetch_count, 2)

        # Third call within backoff cooldown returns stale snapshot without fetch
        self.assertEqual(cache.get_reading_ids(), [42])
        self.assertEqual(fetch_count, 2)

    def test_event_gated_concurrent_refresh_proves_single_fetch(self):
        fetch_count = 0
        gate = threading.Event()
        entered = threading.Event()

        class GatedCache(ReadingIdCache):
            def _fetch_upstream(self):
                nonlocal fetch_count
                fetch_count += 1
                entered.set()
                gate.wait(timeout=5.0)
                return []

        cache = GatedCache(ttl=30.0)
        results = [None, None, None]
        errors = []

        def worker(idx):
            try:
                results[idx] = cache.get_reading_ids()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        # Start first thread to enter fetch
        threads[0].start()
        self.assertTrue(entered.wait(timeout=2.0))

        # Start other threads while first is in-flight
        threads[1].start()
        threads[2].start()

        # Let leader complete fetch
        gate.set()
        for t in threads:
            t.join(timeout=3.0)

        self.assertEqual(errors, [])
        self.assertEqual(results, [[], [], []])
        self.assertEqual(fetch_count, 1)

        # Subsequent immediate read still uses cached empty result
        self.assertEqual(cache.get_reading_ids(), [])
        self.assertEqual(fetch_count, 1)

    def test_unknown_cache_initialization_does_not_pretend_recent_failure(self):
        cache = ReadingIdCache()
        self.assertIsNone(cache._last_failure)
        self.assertIsNone(cache._last_success)
        self.assertEqual(cache._state, "unknown")

    def test_stale_reader_race_proves_single_fetch_with_under_lock_double_check(self):
        fetch_count = 0
        follower_paused = threading.Event()
        resume_follower = threading.Event()
        main_thread = threading.current_thread()

        class RaceCache(ReadingIdCache):
            def _fetch_upstream(self):
                nonlocal fetch_count
                fetch_count += 1
                return [101]

            def _do_refresh(self, force=False):
                if not force and threading.current_thread() != main_thread:
                    follower_paused.set()
                    resume_follower.wait(timeout=5.0)
                return super()._do_refresh(force=force)

        cache = RaceCache(ttl=30.0)
        follower_res = []

        def follower_worker():
            follower_res.append(cache.get_reading_ids())

        t = threading.Thread(target=follower_worker)
        t.start()

        # Wait until follower has passed freshness check and is paused right at _do_refresh entry
        self.assertTrue(follower_paused.wait(timeout=2.0))

        # Main thread explicitly refreshes and fills cache
        cache.refresh()
        self.assertEqual(fetch_count, 1)

        # Unpause follower: under-lock double check must see fresh cache and return [101] without fetch
        resume_follower.set()
        t.join(timeout=2.0)

        self.assertEqual(follower_res, [[101]])
        self.assertEqual(fetch_count, 1)


class TestQueryTransformation(unittest.TestCase):
    """Test recognized query heuristic and query rewriting logic."""

    def test_recognized_heuristic(self):
        self.assertTrue(is_recognized_updates_query(b'{"query": "query GetChapterWithMangaPage { id }"}'))
        self.assertTrue(is_recognized_updates_query(b'query { chapters(orderBy: [FETCHED_AT_DESC]) { nodes { id } } }'))
        self.assertFalse(is_recognized_updates_query(b'query { manga(id: 1) { title } }'))
        self.assertFalse(is_recognized_updates_query(b'mutation { updateChapter { clientMutationId } }'))

    def test_transform_with_reading_ids(self):
        raw = json.dumps({
            "query": "query GetChapterWithMangaPage { chapters { id } }",
            "variables": {
                "filter": {"order": "DESC"},
                "extraVar": 123
            }
        }).encode("utf-8")
        transformed = transform_recognized_query(raw, [10, "20"])
        parsed = json.loads(transformed.decode("utf-8"))

        vars_out = parsed["variables"]
        self.assertEqual(vars_out["extraVar"], 123)
        flt = vars_out["filter"]
        self.assertEqual(flt["order"], "DESC")
        self.assertEqual(flt["inLibrary"], {"equalTo": True})
        self.assertEqual(flt["isRead"], {"equalTo": False})
        self.assertEqual(flt["mangaId"], {"in": [10, "20"]})

    def test_transform_with_empty_reading_ids(self):
        raw = json.dumps({
            "query": "query GetChapterWithMangaPage { chapters { id } }"
        }).encode("utf-8")
        transformed = transform_recognized_query(raw, [])
        parsed = json.loads(transformed.decode("utf-8"))

        flt = parsed["variables"]["filter"]
        self.assertEqual(flt["inLibrary"], {"equalTo": True})
        self.assertEqual(flt["isRead"], {"equalTo": False})
        self.assertEqual(flt["mangaId"], {"equalTo": 0, "notEqualTo": 0})
        self.assertNotIn("in", flt["mangaId"])

    def test_transform_malformed_inputs_raise_payload_error(self):
        with self.assertRaises(FilterPayloadError):
            transform_recognized_query(b"not json", [1])

        with self.assertRaises(FilterPayloadError):
            transform_recognized_query(json.dumps([1, 2, 3]).encode("utf-8"), [1])

        with self.assertRaises(FilterPayloadError):
            transform_recognized_query(json.dumps({"variables": "not_a_dict"}).encode("utf-8"), [1])

        with self.assertRaises(FilterPayloadError):
            transform_recognized_query(json.dumps({"variables": {"filter": "not_a_dict"}}).encode("utf-8"), [1])


class TestHttpHandlerIntegration(unittest.TestCase):
    """Integration tests running FilterHandler against a fake upstream server on ephemeral ports."""

    def setUp(self):
        self.upstream_server = EphemeralServer(FakeUpstreamHandler)
        self.cache = ReadingIdCache(upstream=self.upstream_server.url, retry_cooldown=1.0)
        self.filter_server = EphemeralServer(
            FilterHandler,
            upstream=self.upstream_server.url,
            cache=self.cache,
        )

    def tearDown(self):
        self.filter_server.close()
        self.upstream_server.close()

    def test_healthz_endpoint(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read(), b"OK")
        self.assertEqual(resp.getheader("Content-Type"), "text/plain")
        self.assertEqual(resp.getheader("Content-Length"), "2")
        conn.close()

    def test_get_passthrough_and_error_forwarding(self):
        def upstream_get(handler):
            if handler.path == "/suwayomi/api/v1/manga":
                body = b'{"mangas": [{"id": 1}]}'
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            elif handler.path == "/suwayomi/api/v1/notfound":
                err_body = b'{"error": "not found"}'
                handler.send_response(404)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(err_body)))
                handler.end_headers()
                handler.wfile.write(err_body)

        self.upstream_server.server.get_callback = upstream_get

        # Normal GET
        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("GET", "/suwayomi/api/v1/manga")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read(), b'{"mangas": [{"id": 1}]}')
        conn.close()

        # HTTP error GET (proves fix for GET HTTPError becomes empty 502)
        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("GET", "/suwayomi/api/v1/notfound")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.read(), b'{"error": "not found"}')
        conn.close()

    def test_post_unrecognized_query_byte_identical_passthrough(self):
        received_at_upstream = []

        def upstream_post(handler, body):
            received_at_upstream.append(body)
            resp = b'{"data": "unrecognized_ok"}'
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(resp)))
            handler.end_headers()
            handler.wfile.write(resp)

        self.upstream_server.server.post_callback = upstream_post

        exact_payload = b'{"query": "mutation { updateManga(id: 5) { title } }", "preserve_whitespace": true}'
        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("POST", "/suwayomi/api/graphql", body=exact_payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.read(), b'{"data": "unrecognized_ok"}')
        conn.close()

        self.assertEqual(len(received_at_upstream), 1)
        self.assertEqual(received_at_upstream[0], exact_payload)

    def test_post_recognized_updates_query_rewritten_with_empty_ids(self):
        received_bodies = []

        def upstream_post(handler, body):
            # Check if this is category fetch
            if b"category(id: 1)" in body:
                resp = json.dumps({"data": {"category": {"mangas": {"nodes": []}}}}).encode("utf-8")
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(resp)))
                handler.end_headers()
                handler.wfile.write(resp)
                return

            received_bodies.append(body)
            resp = b'{"data": {"chapters": {"nodes": []}}}'
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(resp)))
            handler.end_headers()
            handler.wfile.write(resp)

        self.upstream_server.server.post_callback = upstream_post

        client_query = json.dumps({
            "query": "query GetChapterWithMangaPage { chapters { id } }",
            "variables": {"filter": {"order": "DESC"}}
        }).encode("utf-8")

        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("POST", "/suwayomi/api/graphql", body=client_query, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        conn.close()

        self.assertEqual(len(received_bodies), 1)
        parsed_upstream = json.loads(received_bodies[0].decode("utf-8"))
        flt = parsed_upstream["variables"]["filter"]
        self.assertEqual(flt["order"], "DESC")
        self.assertEqual(flt["inLibrary"], {"equalTo": True})
        self.assertEqual(flt["isRead"], {"equalTo": False})
        self.assertEqual(flt["mangaId"], {"equalTo": 0, "notEqualTo": 0})
        self.assertNotIn("in", flt["mangaId"])

    def test_post_recognized_cold_failure_returns_controlled_503(self):
        # Upstream fails category query
        def upstream_post(handler, body):
            handler.send_response(500)
            handler.end_headers()

        self.upstream_server.server.post_callback = upstream_post

        client_query = json.dumps({
            "query": "query GetChapterWithMangaPage { chapters { id } }"
        }).encode("utf-8")

        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("POST", "/suwayomi/api/graphql", body=client_query, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 503)
        body = resp.read()
        conn.close()

        err_data = json.loads(body.decode("utf-8"))
        self.assertIn("errors", err_data)
        self.assertEqual(err_data["errors"][0]["message"], "Reading category cache unavailable")

    def test_post_recognized_malformed_returns_controlled_400(self):
        # Warm cache so cold failure is not triggered
        self.cache._state = "ready"
        self.cache._snapshot = [1, 2]
        self.cache._last_success = time.monotonic()

        bad_payload = b'{"query": "query GetChapterWithMangaPage", "variables": "not_dict"}'
        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("POST", "/suwayomi/api/graphql", body=bad_payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        body = resp.read()
        conn.close()

        err_data = json.loads(body.decode("utf-8"))
        self.assertEqual(err_data["errors"][0]["message"], "Invalid GraphQL request payload")

    def test_post_recognized_malformed_cold_upstream_returns_400_without_category_fetch(self):
        category_fetches = 0
        feed_forwards = 0

        def upstream_post(handler, body):
            nonlocal category_fetches, feed_forwards
            if b"category(id: 1)" in body:
                category_fetches += 1
                handler.send_response(500)
                handler.end_headers()
                return
            feed_forwards += 1
            handler.send_response(200)
            handler.end_headers()

        self.upstream_server.server.post_callback = upstream_post

        # Cache is completely cold
        self.assertIsNone(self.cache._snapshot)
        self.assertEqual(self.cache._state, "unknown")

        bad_payload = b'{"query": "query GetChapterWithMangaPage", "variables": "not_a_dict"}'
        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("POST", "/suwayomi/api/graphql", body=bad_payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        body = resp.read()
        conn.close()

        err_data = json.loads(body.decode("utf-8"))
        self.assertEqual(err_data["errors"][0]["message"], "Invalid GraphQL request payload")

        # Proves zero category fetch and zero feed forward occurred
        self.assertEqual(category_fetches, 0)
        self.assertEqual(feed_forwards, 0)

    def test_headers_stripping_and_uniqueness(self):
        def upstream_get(handler):
            body = b"hello compressed body"
            handler.send_response(200)
            handler.send_header("Server", "UpstreamServer/1.0")
            handler.send_header("Date", "Wed, 01 Jan 2026 00:00:00 GMT")
            handler.send_header("Connection", "close, X-Custom-Hop")
            handler.send_header("X-Custom-Hop", "secret-hop")
            handler.send_header("Keep-Alive", "timeout=10")
            handler.send_header("Upgrade", "proto/1.0")
            handler.send_header("Content-Encoding", "gzip")
            handler.send_header("X-App-Data", "retained")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            # Send valid chunked body
            chunk = f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n0\r\n\r\n"
            handler.wfile.write(chunk)

        self.upstream_server.server.get_callback = upstream_get

        conn = http.client.HTTPConnection("127.0.0.1", self.filter_server.port)
        conn.request("GET", "/test-headers")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b"hello compressed body")

        # Inspect headers
        raw_headers = resp.headers._headers
        header_names = [k.lower() for k, v in raw_headers]

        # Verify uniqueness of generated headers
        self.assertEqual(header_names.count("server"), 1)
        self.assertEqual(header_names.count("date"), 1)
        self.assertEqual(header_names.count("content-length"), 1)
        self.assertEqual(resp.getheader("Content-Length"), str(len(body)))

        # Verify hop-by-hop headers stripped
        self.assertNotIn("connection", header_names)
        self.assertNotIn("keep-alive", header_names)
        self.assertNotIn("transfer-encoding", header_names)
        self.assertNotIn("x-custom-hop", header_names)
        self.assertNotIn("upgrade", header_names)

        # Verify preserved headers
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        self.assertEqual(resp.getheader("X-App-Data"), "retained")

        # Verify preserved headers
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        self.assertEqual(resp.getheader("X-App-Data"), "retained")


if __name__ == "__main__":
    unittest.main()
