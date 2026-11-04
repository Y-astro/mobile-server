# Material 3 home-server portal design

Date: 2026-11-04
Status: Proposed for user review. No redesign implementation has started.

## Scope and intent

The user requested Material 3 for the website and a more deliberate teal and gold palette. Apply the design to administrator, family and login pages, including dynamically rendered service/session cards, notes controls and conversion dialogs. This visual scope supersedes the earlier appearance-preservation requirement. All services, roles, routes, data, authentication and recently verified polling behavior remain compatible. General-server stabilization continues independently; deep Suwayomi work remains later.

## Existing-screen audit

The High AGY audit rendered maintained pages with synthetic local fixtures and blocked external traffic. Root inspected the screenshots and source findings. Current presentation has gold gradients/glows, deeply tinted green surfaces, serif telemetry headings, small controls and competing Tailwind/custom declarations. Authored CSS contains many important overrides. Static and dynamically generated controls do not share one clear component vocabulary.

Baseline screenshots are audit evidence, not live server measurements. The admin fixture shows unavailable readings and lacks the populated dynamic service grid, so its screenshot alone cannot establish final service-card proportions. Implementation acceptance must render the complete grid with representative synthetic states. Inline percentage widths are dynamic data bindings and must remain functional; they are not merely decorative style clutter.

## Direction

- Product: a private household server console hosted on a Redmi K20 Pro, serving family tools and administrator controls.
- Character: calm, tactile, dependable.
- World: a quiet appliance console with neutral dark surfaces and readable hardware indicators.
- Moments: a focused telemetry cluster and a practical grid of service launchers with explicit status chips.
- Signature: a restrained K20 Pro node identity and observed connection/battery status. Unknown readings remain unknown; do not invent online, encryption or availability claims.

## Recommended approach

Use full Material 3 component styling with semantic shared classes and design tokens. Replace misleading glass/gradient naming and competing visual declarations across markup and dynamic templates. Keep existing functional IDs, form fields, routes, event hooks and behavior. Where legacy classes are needed as compatibility hooks, retain them deliberately without a competing visual implementation.

A lighter alternative would recolor existing classes. It needs fewer markup edits, but retains the confusing styling ownership that prompted the refactor. The full component approach is recommended. No new frontend framework, Material component package or mandatory external font/CDN dependency is needed.

## Palette and component rules

Default to the current dark appearance. A light theme is an optional user decision, not assumed scope.

| Role | Color | Paired text | Measured contrast |
| --- | --- | --- | --- |
| Main surface | #111414 | #e1e3e3 | 14.37:1 |
| Card surface | #1d2223 | #e1e3e3 | 12.49:1 |
| Primary teal action | #70d6c5 | #003734 | 7.59:1 |
| Teal container | #004f4a | #8bf2e1 | 7.16:1 |
| Restrained warm gold | #e5c46c | #3c2f00 | 7.78:1 |
| Gold container | #574400 | #ffe08b | 7.30:1 |

Use Material color roles through shared --md-sys-color-* variables. Teal identifies primary actions and selections. Gold provides occasional warm hardware/secondary emphasis; it does not compete on every surface. Neutral tonal steps establish depth. Red remains reserved for errors and destructive states.

Use a system/Roboto sans stack and tabular figures. Keep a 4px spacing rhythm, readable body text and sentence-case labels. Use consistent Material shapes: medium cards, larger grouped panels and dialogs, and rounded action controls. All interactive targets need at least a 48px touch area, visible keyboard focus and distinct disabled/pending/error states. Keep one icon style. Use modest state layers and short transitions with reduced-motion support; remove decorative gradients, glows and blur effects.

Desktop keeps the existing section hierarchy with fluid telemetry/service layouts. Mobile stacks sections in reading order, fits 360–412px widths without horizontal overflow, and keeps core service actions easy to reach. Family pages stay task-focused; administrator-only controls remain restricted. Login uses matching fields, button hierarchy and error treatment.

## Ownership and preservation

Shared CSS owns tokens, layout and component presentation. Templates own role-specific structure. Dynamic service, AGY and laptop renderers use the same component classes. Polling owns request lifecycle and never owns visual theme state. Preserve notes drafts, selected files, conversion downloads and active terminal behavior across refresh and lifecycle events.

No auth/session storage change, service-control API change, new status endpoint or data migration belongs in this redesign. Preserve existing role separation and missing-template failure behavior. Source and release ownership must include every changed asset. The live site changes only through the reviewed server release mechanism.

## Acceptance

Render actual maintained admin, family and login templates with representative synthetic data at desktop and mobile sizes. Verify colors, typography, spacing, all service cards, notes and conversion dialogs. Check keyboard focus, dialog Escape/focus behavior, reduced motion, contrast and 48px touch areas.

Run the existing actual-page browser suites and focused visual/accessibility checks. Preserve login/error/logout, role denial, service toggles and links, AGY/laptop creation and closure, notes saving and formatting, conversion input/removal/download, and polling failure/deadline/resume behavior. No functionality is removed to simplify the design.

## References and review

The design follows Material color-role principles: https://m3.material.io/styles/color/the-color-system/key-colors-tones and https://m3.material.io/styles/color/roles . Touch-target guidance: https://developer.android.com/develop/ui/compose/accessibility/api-defaults . Palette choices are custom brand decisions; root independently calculated the listed contrast ratios.

The requested brainstorming and unslop workflows require reviewing the new direction before visual implementation. One approval of this written direction covers the redesign scope; do not repeatedly request approval for routine component or implementation choices within it. After approval, create the bounded implementation plan and use Gemini 3.8 Flash High agents for execution, with Codex verification.
