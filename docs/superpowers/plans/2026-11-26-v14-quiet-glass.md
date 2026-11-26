# V14 Quiet Glass Dashboard

## Intent and direction

Implement the `14 · Quiet Glass` variant in `/home/astro/Downloads/k20-v14-v15-review.html` for the admin and family dashboards. The page's other variant is `15 · Instrument Console`; it is excluded. The existing live server remains the source of telemetry and service state.

Product: a private server console and family portal. Character: quiet, precise, compact. Visual world: muted glass panels with lightly ruled instruments. Preserve: Matugen wallpaper palette, appearance access, roles, compact controls and working session actions. Remove visual noise through flat regions, restrained icons, underline filters and a ruled remote column.

## Boundaries

Keep the user's Matugen-only palette and adjustable wallpaper/blur/transparency; do not copy the preview's fixed colour presets, fake metrics, wallpaper payload, design chooser, sample users or illustrative chart values. A question is pending about building real history; implement styling independently. Until answered, do not add a fabricated history panel or a history backend.

This task owns dashboard presentation only: `web/portal.css`, minimal role-template markup in `web/admin.html` and `web/family.html`, and focused browser expectations. Keep navigation, notes, account permissions, service status/filter/reorder/columns and terminal operations intact. Preserve unknown/static family state and on-demand Ready as neutral. Standalone service interfaces, runtime scripts, Caddy, gateway and data are outside this style change.

## Execution

1. Render the exact Quiet Glass reference at matching desktop/mobile sizes; compare final computed rules, including later overrides. Capture representative screenshots outside Git. Adapt reference values to the existing Matugen tokens and real templates.
2. Restyle the dashboards: flat transparent health regions with divider rules; quieter header; underlined service filters; muted compact tiles and icons; subtle verified-running fills; 315px ruled remote column with visible heading, unboxed sessions and quiet controls. Use the reference's responsive stack at 1050px and mobile rules. Keep 32px launch icons and compact standard buttons unless a clear v14 reference value changes them.
3. Update actual-page browser assertions for the intended design. Preserve all interaction and role tests. Check 1440/1280/1050/768/390/320 widths, long labels, column preferences, focus/disclosures and service mutations with isolated data. Inspect before/after screenshots and commit bounded source.
4. Root reviews the agent diff and verifies acceptance. Build the complete committed candidate manifest, recapture live preconditions, reject unexplained drift, and activate only changed owned assets through the reviewed release mechanism. Preserve all disabled markers, user data and excluded processes.
5. Verify both live roles and served assets after detachment. Update deployment evidence and the architecture map, integrate into main and push under the existing authorization. Document real test limits.

## Baseline

Maintained and implementation worktrees start at `fd854400be303f4db088d8f9a4033dd698e8496c`. Root read-only verification on 2026-11-26 matched all 62 live manifest destinations to the previous release. Seven disabled markers and process identities are captured privately in `/home/astro/server-audit/2026-11-26/v14-review/live-before.json`.

## Root acceptance correction

The worker committed `937b643`. Root reran the actual-template suite and confirmed the visual change. A new opacity regression reproduced fixed 48%/45% tile fills ignoring the existing transparency setting. Root changed those tints to remain proportional to `--glass`, preserving the reference values at the default 65% panel opacity and allowing fully transparent cards at zero. The optional history scope question remains unanswered; this release implements the visual variant and contains no sample charts.

Root also ran the older default material3 browser gate. Its blanket 48px dashboard assertions conflicted with the user's explicit compact controls and v14's 42×25px switches. Dashboard target checks now enforce a 24px minimum while modal/login checks retain 48px. The All filter uses the reference label All services and a minimum width so the underline-style control does not become a 14px target. Contrast, keyboard, role and action assertions remain.

## Completed publication

Final source `c4529622d602` is live through completed transaction `act_9000f22516ca`. Root verified both roles, the complete 62-entry release, preserved disabled state/process identities, actual responsive layouts and the default npm gate. See [deployment report](../v14-quiet-glass-deployment.md). The optional real-history feature remains outside this visual release.
