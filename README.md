# Redmi mobile server

Phone-side recovery and a private Tailscale-accessible dashboard, FileBrowser, persistent AGY terminal, and optional Tor SOCKS proxy for an existing Termux server.

- Salted password hashes, bounded login verification and admin route authorization.
- Six parallel browser files with streaming server admission, TUS write locking and body/conversion limits.
- Measured telemetry with explicit unavailable/stale values.
- Real verified Tor proxy; full-device Tor routing is unavailable.
- tmux response scrolling and persistent sessions; supervisor recovery respects deliberate stops.

See [operations and setup](docs/OPERATIONS.md), [measured performance](docs/PERFORMANCE.md), and [third-party notices](licenses/README.md). Runtime credentials, keys, databases and user files are excluded.

## Checks

```sh
python3 -m unittest discover -s tests -v
(cd gateway && go test -race ./...)
npm ci
npx playwright install chromium
npm test
```

Set `CHROMIUM_PATH` when using an existing Chromium executable. Live phone checks used SSH/ADB and private temporary fixtures; no phone credentials are included in the tests.
