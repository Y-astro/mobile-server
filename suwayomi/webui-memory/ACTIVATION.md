# Suwayomi custom WebUI activation

The backend copies `data/webUI` into its serving directory at startup. The native `CUSTOM` WebUI flavor prevents upstream validation from replacing the maintained UI. Publication changes only that setting and the complete UI directory; it preserves the backend JAR, DNS shim, disabled flags, portal files and unrelated services.

`tools/publish_suwayomi_webui.py` requires separately reviewed archive and provenance SHA-256 digests plus the complete prior tree and configuration digest. It validates every archive file before service signals, retains older lazy assets, requires native atomic directory exchange, and holds the existing services lock across exact backend shutdown, publication and health checks. There is no two-rename fallback. Configuration bytes and mode are preserved except the flavor value. Backups and journals use private permissions.

The private transaction remains at `suwayomi/data/.webui_staging`. A second publication refuses to overwrite it. `--recover` restores the prior release, including after a successful publication. It derives interrupted exchange state from both directory manifests and refuses recovery if configuration, files, protected runtime or disabled-service intent drifted. Neither normal publication nor recovery guesses over concurrent edits. Process interruption immediately after exchange and ordinary failed-health rollback are covered by real temporary filesystem tests with an injected lifecycle driver.

Publication arguments: `--zip`, `--provenance`, `--expected-sha256`, `--expected-provenance-sha256`, and `--preconditions`. Preconditions JSON contains `tree` (relative paths to SHA-256) and `conf_sha256`. `--home-dir` defaults to the Termux home. Recovery requires only `--recover` and the intended home. Never print or commit the real server configuration or backup.

Root acceptance still requires native phone tests, artifact/source verification, real activation, routed browser behavior without response overrides, comparable timing, and detached service health. Filesystem durability does not guarantee survival of failing storage hardware.
