# Expressive Workbench redesign

## Direction and authorization

The user supplied `/home/astro/Downloads/k20-v10-expressive-workbench.html` and explicitly requested planning followed by execution. Their scope clarification includes every interface except the standalone Suwayomi, Wealthfolio, and Navidrome applications. The dashboard cards linking to those three applications are included.

Use the rendered reference, not its obsolete CSS declarations. Coordinating-agent screenshots and computed-style checks are recorded in `/home/astro/server-audit/2026-11-24/expressive-redesign/reference-verified.md`. The reference contains no rendered navigation rail or mobile bottom navigation.

Product: a phone-hosted server workbench for its administrator and family.
Character: calm, tactile, instrument-like.
World: readable instruments and controls against the user's wallpaper.
Moments: the resource instrument panel and stateful service cards.
Signature: consistent SVG glyphs, status dots, rounded surfaces, and pill controls.

## Required appearance

- A wide header with account identity, live uptime, and allowed account actions.
- Four resource tiles with a unified network panel underneath.
- A default three-column service workbench on desktop and a roughly 320px tools column alongside it. The tools column stacks below on narrower viewports.
- A service-column preference with values 1–4, clamped responsively to usable card widths. At approximately 390px, the reference fits two cards without horizontal overflow.
- Service filtering, an explicit arrange mode, pointer reordering, keyboard move controls, and a reset action, integrated with existing live actions and stored order.
- Outer surfaces around 24px radius, smaller service surfaces around 12–16px, concise labels, and a system/Roboto font stack. Use the active reference cascade for exact spacing and typography.
- Preserve wallpaper selection, blur and translucency preferences. Colours come from existing Matugen-derived tokens; do not install static palette presets or replace the user's wallpaper with the prototype's embedded image.
- A consistent component treatment for buttons, switches, chips, inputs, tables, empty/error/loading states, dialogs, notifications, and service toolbars across all included surfaces.

## Functional and account boundaries

- Preserve every current route, API contract, service action, and current live feature, including Repertoire Finder.
- Render real telemetry. Missing values stay unavailable; never copy the reference's sample readings, preview labels, or hardware-protection claims.
- Preserve backend authorization and existing per-account data visibility. Appearance customization remains admin-only. Family access follows actual current backend and template contracts.
- Keep administrator remote tools and notes in the tools column. Exactly one AGY panel may contain multiple sessions; do not cap sessions to one. Preserve laptop terminal creation, reconnect, close, and power controls.
- Retain personal/shared music isolation. Do not modify the excluded applications' internal interfaces, databases, runtime settings, or source bundles.
- Preserve current enabled and disabled service intent. Do not start disabled services to obtain screenshots. Headless services receive consistent dashboard controls rather than invented standalone UIs.
- Preserve existing downloader queue removal, retry, duplicate warnings, priority ordering, drag controls, and download destination semantics.
- Preserve Repertoire search, filters, sorting, details, workbook-version selection, validation, and upload authorization. Workbook contents are never part of this redesign or a source commit.

## Source ownership and rollout

Work in the isolated `ui/expressive-workbench` Git worktree. Reconcile the verified live Repertoire code before changing its appearance. The main checkout's untracked gateway artifact and Graphify cache are unrelated and must remain untouched.

Keep shared tokens and common component styling in existing shared CSS owners. Keep service-specific rules in the service stylesheet. Do not accumulate competing override stacks or replace working service/backend implementations with prototype mocks.

The primary project covers dashboard, login, appearance, converter, notes, Antra, Repertoire, Cloud Storage, and terminal wrappers. Native AdGuard, Syncthing, and Vaultwarden interfaces require a separate, evidence-based adapter plan once their installed assets and supported branding hooks are inventoried. Those surfaces are in scope, not silently deferred; the adapter plan must be written before editing them. Avoid an extra theme proxy or new daemon when native/static asset hooks suffice.

Before live publication, commit maintained source and validate a reviewed release manifest against fresh live preconditions. Preserve newer runtime files, credentials, notes, workbooks, databases, and disabled flags. Restart only affected services. Keep SSH and excluded services available. Prepare recovery before activation.

## Acceptance

- Compare rendered admin and family pages to the reference at desktop and mobile widths.
- Check widths 390, 768, 1280, and 1440px with no document overflow, readable long titles, and usable controls. Retain 48px touch targets through hit areas where needed.
- Verify live service launches, toggle rollback, in-flight updates, ordering/filter/column persistence, and all remote session actions against actual role templates.
- Verify notes save status, converter interactions, appearance persistence, downloader actions, and Repertoire operations with isolated fixtures; avoid destructive production data tests.
- Verify keyboard access, visible focus, native dialog labelling and dismissal, reorder announcements, reduced motion, and readable surfaces across supported wallpaper settings.
- Verify native service styles on accessible live pages or isolated installed-asset previews for disabled services, without changing their enabled state.
- Complete role-boundary and deployment checks, then verify the published pages and refresh architecture documentation. Report any surface that cannot be verified rather than claiming universal coverage.

## Scope Addendum: Standalone Native Application UI Exclusions

Following explicit user cancellation, the native redesign for AdGuard Home, Syncthing, and Vaultwarden is cancelled. All six standalone native application UIs (Suwayomi, Wealthfolio, Navidrome, AdGuard Home, Syncthing, and Vaultwarden) are excluded from custom standalone UI redesign. All six retain their existing interfaces and service commands. Dashboard cards and remote controls linking to these services remain fully in scope.
