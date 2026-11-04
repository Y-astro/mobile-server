# Release Mode Policy: Audited Destination Overrides

## Context & Defect Correction
Under the prior coarse pattern-matching in `get_canonical_target_mode`, files residing in `home/bin/` were unconditionally forced to mode `0700`, while any path ending in `/usr/local/bin/agy-runner-loop` was forced to `0755`, and `.config/` assets were forced to `0600`.
On the audited live phone environment:
1. The Debian PRoot container runner (`/data/data/com.termux/files/usr/var/lib/proot-distro/containers/debian/rootfs/usr/local/bin/agy-runner-loop`) is legitimately mode `0700`.
2. Service launchers and scripts (`agy_session.sh`, `termux_session.sh`, `start_suwayomi.sh`, `suwayomi_daily_update.py`, `suwayomi_graphql_filter.py`, `suwayomi_update.sh`) are legitimately mode `0755`.
3. The ttyd terminal index asset (`/data/data/com.termux/files/home/.config/ttyd/termux_index.html`) is legitimately mode `0644`.

The prior heuristic forced mode-only `REPLACE` actions on byte-identical live files, causing activation checks to refuse deployment when Suwayomi or AGY services were preserved.

## Canonical Policy Rule
Canonical modes are derived strictly from a centralized destination override table (`AUDITED_TARGET_MODE_OVERRIDES` in `scripts/release_transaction.py`), retaining standard defaults elsewhere:
- `/data/data/com.termux/files/usr/var/lib/proot-distro/containers/debian/rootfs/usr/local/bin/agy-runner-loop`: `0700`
- `/data/data/com.termux/files/home/bin/agy_session.sh`: `0755`
- `/data/data/com.termux/files/home/bin/start_suwayomi.sh`: `0755`
- `/data/data/com.termux/files/home/bin/suwayomi_daily_update.py`: `0755`
- `/data/data/com.termux/files/home/bin/suwayomi_graphql_filter.py`: `0755`
- `/data/data/com.termux/files/home/bin/suwayomi_update.sh`: `0755`
- `/data/data/com.termux/files/home/bin/termux_session.sh`: `0755`
- `/data/data/com.termux/files/home/.config/ttyd/termux_index.html`: `0644`

Any manifest or precondition declaring non-canonical modes is rejected upfront before any staging or journal operations.
