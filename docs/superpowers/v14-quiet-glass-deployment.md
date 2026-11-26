# V14 Quiet Glass Deployment

The admin and family dashboards now use the `14 · Quiet Glass` variant from `/home/astro/Downloads/k20-v14-v15-review.html`. Matugen wallpaper colours, existing transparency/blur controls, account permissions, notes and session operations remain. V15, review-only theme/design pickers and illustrative telemetry charts are excluded.

## Result and ownership

- Flat glass instrument regions, ruled metrics, 24px network readings, quiet header/profile and underline service filters.
- Compact muted service cards, unboxed icons and subtle accent fills only for verified running state. Ready and static unknown family state remain neutral. The opacity of both neutral and active tiles follows the existing glass setting.
- Visible Remote access heading, 315px ruled sidebar, quiet session controls, 42×25px dashboard switches and compact notes. Layout stacks at 1050px; mobile health/network remain two columns.
- Production ownership changed only in `web/admin.html` and `web/portal.css`. Family inherits scoped dashboard styling. APIs, data, Caddy, gateway, runtime scripts, standalone service interfaces and terminal transport were not modified.

## Source and release

AGY High implemented commit `937b643abe5457f7c857b862471634de34bab700`. Root reviewed the actual diff and reference screenshots, then fixed transparency propagation in `943d0c55c6ba17cf8d22d02e03d8284535d8e156`. Root aligned the default browser gate with the user-requested compact controls and gave All services its reference label/minimum width in `c4529622d60263abdca7e0f99bd98a3fd5fea8ee`.

The first scoped activation `act_2d55d883dbbb` completed. Final source `c4529622d60263abdca7e0f99bd98a3fd5fea8ee` is live through completed transaction `act_9000f22516ca`. Both transactions replaced only the two owned UI assets, leaving 60 entries untouched and restarting dashboard only. No live shell session was created or closed.

## Root verification

- `npm test` passed the default fixture/browser/dashboard/memory/laptop/polling/material3 chain, all 11 Node polling tests, and the installer fail-fast guard.
- Actual-template expressive browser suite passed all 12 blocks at 1440, 1280, 1050, 768, 390 and 320px. Sidebar service-control suite passed seven blocks; dedicated laptop create/reconnect/close/offline/error-recovery checks passed. Python role-template and release-manifest suites passed 32 tests.
- Root reproduced the fixed-opacity bug with a zero-opacity regression before fixing it. Both service states now become transparent when the existing glass setting reaches zero. Native focus, dialog controls and standalone login/form sizes retain their existing assertions; dashboard targets use the intentional compact contract with a 24px minimum.
- Live browser checks passed for admin/family portal, Cloud, role gates, intrinsic column changes, opacity, keyboard disclosure and laptop HTML. Observed the 315px sidebar, transparent square metric regions and 2px active-filter underline. No document overflow or uncaught page errors occurred.
- Post-activation verification matched all 62 destination hashes, including the unchanged boot-script symlink target. All seven disabled-marker records retained their metadata. Every observed process except dashboard retained its PID/start identity after detached activation and browser verification.

Private evidence: `/home/astro/server-audit/2026-11-26/v14-review/`, including reference/implementation screenshots, `root-*.log`, baseline receipts and `final-release/final-proof.json`. Live screenshots mask private notes. Credentials/signing keys were never printed or committed; temporary role sessions are removed after verification.

## Limits

Real telemetry history was not implemented while the optional scope choice remained unanswered. No illustrative series is shown as real data. AGY/Termux remained disabled and live shell sessions were not opened for acceptance. Standalone applications retain their existing designs and runtime intent. Graphify's machine graph remains the dated Nov 20 baseline; the human-readable system map reflects this UI release.
