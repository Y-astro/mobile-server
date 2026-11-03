# Dashboard Conversion and Served-Page Contract Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for bounded execution and independent review.

**Goal:** Make both documented conversion URLs work with identical safeguards, and test the actual maintained role pages.

**Architecture:** Normalize the known alias at the shared admission boundary and route dispatch. Switch browser fixtures from embedded HTML to maintained web templates. Preserve runtime behavior outside the confirmed bug and current UI appearance.

**Tech Stack:** Python stdlib server/unittest, plain HTML/JavaScript and existing Playwright.

**Spec:** docs/superpowers/specs/2026-11-03-redmi-server-stabilization-design.md

## Global Constraints

- Preserve current appearance, user data, roles, cookies, existing endpoints and operational behavior.
- Phone access is prohibited in this task; no credentials, installs, restarts, deployment or user-library operations.
- Work only in this branch. Auth and gateway repairs run independently in separate worktrees under the approved design; do not modify their files.
- Read/apply /home/astro/.agents/skills/unslop/SKILL.md for interface work. Modern-web guidance was retrieved before this task at /home/astro/server-audit/2026-11-03/modern-web-guidance.md.
- Keep historical config/deployment-files.json unchanged; it records installed-source preconditions.

### Task 1: Align conversion enforcement and exercise actual pages

**Files:** Modify app/limits.py, app/dashboard.py (only conversion normalization/import/admission/dispatch), scripts/browser-fixture.py, tests/test_dashboard.py, tests/browser.cjs, tests/laptop-browser.cjs and package.json if required. Create tests/dashboard-browser.cjs when useful. Do not edit app/auth.py or tests/test_auth.py, gateway/, service memory code, page styling/layout or shared service/proxy configuration.

**Interfaces:**

```python
CONVERSION_PATHS = frozenset(("/api/convert", "/api/convert/pdf"))
def canonical_post_path(path):
    return "/api/convert" if path in CONVERSION_PATHS else path
```

Normalize before BODY_LIMITS selection, Admission.acquire and conversion route dispatch, sharing the exact same semaphore. Avoid globally modifying self.path and preserve unrelated query/path behavior.

- [ ] Add focused failing HTTP-handler/admission tests for both aliases: authenticated admin/family success with tiny multipart fixture and mocked converter; unauthenticated denial; identical oversized-body and input-count rejection; shared busy slot; release after failure. Use disposable HOME/test users only.
- [ ] Record red proof, then implement narrow normalization and repeat focused tests. Existing converter signature is convert_documents_batch(file_list) returning (out_bytes, out_name, mime_type, err). Existing response uses Content-Type, Content-Disposition attachment filename, Content-Length and raw PDF/ZIP bytes. Preserve multipart file/files/files[] support and 32MiB/10-input limits.
- [ ] Make scripts/browser-fixture.py copy web/admin.html to .work/dashboard-test.html and produce family/login fixtures from maintained templates as needed. Do not extract old embedded strings or alter them merely to satisfy old tests.
- [ ] Keep existing AGY/laptop user-interaction checks, updating only assertions reflecting captured current pages (Wi-Fi link visible host:port text). Add actual admin/family conversion file-selection/button/download checks observing URL/form-data/headers/output filename, and notes load/edit/save checks against synthetic API fixtures. Login page must load from maintained source. Prevent tests from contacting real mutation APIs.
- [ ] Run focused DashboardTests and browser suite with NODE_PATH=/home/astro/repos/mobile-server/node_modules CHROMIUM_PATH=/home/astro/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome. The inherited auth freshness failure in full Python suite is owned by a separate worker; do not weaken it or claim it passes here. No Go tests needed.
- [ ] Run git diff --check; commit scoped source/tests/fixture changes. Write a concise report containing red/green evidence, actual commands, commits and remaining scope. No deployment.

Acceptance: both routes execute the same safeguarded operation; actual maintained admin/family pages drive expected conversion and notes contracts; existing AGY/laptop checks pass; UI appearance remains unchanged; authentication code and memory reporting remain untouched for independent integration.
