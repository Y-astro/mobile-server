"""Exercise the actual thumbnail proxy against slow and failed image responses."""
import inspect
import json
import os
from pathlib import Path
import shlex
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def exercise_thumbnail_proxy(block):
    import base64
    from concurrent.futures import ThreadPoolExecutor
    import http.server
    import json
    import pathlib
    import socket
    import subprocess
    import tempfile
    import threading
    import time
    import urllib.error
    import urllib.request

    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jp1kAAAAASUVORK5CYII=')

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            kind = self.path.split('?', 1)[0].split('/')[-2] if '/manga/' in self.path else 'graphql'
            if kind == '6':
                time.sleep(18)
            status = {'3': 500, '4': 404}.get(kind, 200)
            body = png if kind in ('2', '6') else (b'{"data":{"ready":true}}' if kind == 'graphql' else b'upstream failure')
            self.send_response(status)
            self.send_header('Content-Type', 'image/png' if kind in ('2', '6') else 'text/html')
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='suwayomi-proxy-test-') as temporary:
            root = pathlib.Path(temporary)
            (root / '1.png').write_bytes(png)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            upstream = f'127.0.0.1:{server.server_port}'
            block = block.replace('/data/data/com.termux/files/home/suwayomi/data/downloads/thumbnails', str(root))
            block = block.replace('127.0.0.1:4569', upstream)
            config = root / 'Caddyfile'
            config.write_text('{\n admin off\n persist_config off\n auto_https off\n}\n' + f'http://127.0.0.1:{port} {{\n' + block + f'\nhandle /suwayomi/api/graphql {{\n reverse_proxy {upstream}\n}}\n}}\n')
            with (root / 'caddy.log').open('w+') as log:
                process = subprocess.Popen(['caddy', 'run', '--config', str(config), '--adapter', 'caddyfile'], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                try:
                    for _ in range(60):
                        if process.poll() is not None:
                            log.seek(0)
                            raise AssertionError(log.read()[-1500:])
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                                break
                        except OSError:
                            time.sleep(.05)
                    else:
                        raise AssertionError('Isolated Caddy did not start')

                    def fetch(path):
                        started = time.monotonic()
                        try:
                            response = urllib.request.urlopen(origin + path, timeout=22)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            data = response.read()
                            return {'status': response.status, 'cache': response.headers.get('Cache-Control', ''), 'seconds': time.monotonic() - started, 'bytes': len(data)}

                    results = {str(i): fetch(f'/suwayomi/api/v1/manga/{i}/thumbnail') for i in range(1, 6)}
                    assert results['1']['status'] == 200 and 'immutable' in results['1']['cache'], results['1']
                    assert results['2']['status'] == 200 and 'immutable' in results['2']['cache'], results['2']
                    for key, status in [('3', 500), ('4', 404), ('5', 200)]:
                        assert results[key]['status'] == status and results[key]['cache'] == 'no-store', results[key]
                    # Six delayed covers model a client's bounded request pool; Reading must eventually progress.
                    started = time.monotonic()
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        covers = [pool.submit(fetch, f'/suwayomi/api/v1/manga/6/thumbnail?slot={i}') for i in range(6)]
                        reading = pool.submit(fetch, '/suwayomi/api/graphql')
                        result = reading.result(timeout=17.8)
                        assert result['status'] == 200, result
                        assert time.monotonic() - started < 17.8, 'Reading remained starved behind covers'
                        for job in covers:
                            failed = job.result()
                            assert failed['status'] in (502, 504), failed
                            assert failed['cache'] == 'no-store', failed
                    # Abandon a cold cover, then verify a cached cover still responds promptly.
                    abandoned = socket.create_connection(('127.0.0.1', port), timeout=2)
                    abandoned.sendall(b'GET /suwayomi/api/v1/manga/6/thumbnail HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    abandoned.close()
                    assert fetch('/suwayomi/api/v1/manga/1/thumbnail')['seconds'] < 1
                    print(json.dumps({'cached_image': results['1'], 'cold_image': results['2'], 'errors_and_nonimages_no_store': True, 'six_slow_covers_bounded': True, 'reading_progress': True, 'cancelled_cover_does_not_block_cache': True}))
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    finally:
        server.shutdown()
        server.server_close()


@unittest.skipUnless(os.environ.get('CADDY_TEST_SSH') == '1', 'Opt in to isolated phone Caddy runtime test with CADDY_TEST_SSH=1')
class TestThumbnailProxyRuntime(unittest.TestCase):
    def test_real_thumbnail_route_handles_failures_and_request_churn(self):
        text = (ROOT / 'config/Caddyfile').read_text().split('http://:4567 {', 1)[1]
        block = text[text.index('\t@mangaThumbnail'):text.index('\n\t@graphqlPost')]
        program = inspect.getsource(exercise_thumbnail_proxy) + '\nimport json,sys\nexercise_thumbnail_proxy(json.load(sys.stdin))\n'
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-p', '8022', 'u0_a505@100.75.159.53', 'python -B -c ' + shlex.quote(program)], input=json.dumps(block), text=True, capture_output=True, timeout=50)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        proof = json.loads(result.stdout)
        self.assertTrue(proof['reading_progress'])
        self.assertTrue(proof['errors_and_nonimages_no_store'])


if __name__ == '__main__':
    unittest.main()
