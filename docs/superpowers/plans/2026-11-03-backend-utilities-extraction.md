# Backend Utilities Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for bounded execution and independent review.

**Goal:** Isolate notes persistence and document conversion from the dashboard monolith while preserving current HTTP and storage contracts.

**Architecture:** Move existing behavior into two stdlib modules, with explicit testable storage paths. Keep dashboard bindings as compatibility seams for existing request handlers/tests. No route framework, state-format migration or feature change.

**Tech Stack:** Python standard library, existing unittest and actual-page Playwright checks.

**Spec:** docs/superpowers/specs/2026-11-03-redmi-server-stabilization-design.md

## Global Constraints

- Preserve routes, auth roles, cookie behavior, response fields, notes JSON schema, conversion output and failure behavior.
- No phone access/writes, credentials, actual LibreOffice execution, package installs, service changes, deployments or other agents.
- Preserve all existing comments and docstrings when moving code. No visual changes.
- Only extract these utilities; do not touch auth, service-memory logic, Caddy, gateway, polling, embedded templates or supervisor.

### Task 1: Extract notes storage and conversion behavior

**Files:** Create app/notes.py, app/document_conversion.py, tests/test_notes.py, tests/test_document_conversion.py. Modify app/dashboard.py only for extracted definitions/imports/bindings, tests/test_dashboard.py only for notes HTTP contract coverage, and docs/SOURCE-OWNERSHIP.md to identify new owners.

**Interfaces:**

```python
class NotesStore:
    def __init__(self, path, *, lock=None): ...
    def get(self, username): ...
    def set(self, username, content): ...

# dashboard bindings preserve existing caller names:
NOTES_STORE = NotesStore(NOTES_FILE, lock=NOTES_LOCK)
def get_user_notes(username):
    return NOTES_STORE.get(username)
def set_user_notes(username, content):
    return NOTES_STORE.set(username, content)
```

Methods keep current empty-user results, string handling, JSON username->notes mapping,600 file mode, shared locking, atomic .tmp replacement/cleanup and error behavior. Constructor must not create/read files or start threads. Existing dashboard NOTES_FILE/NOTES_LOCK remain available.

```python
DEFAULT_CONVERSION_ROOT = "/data/data/com.termux/files/home/data/conversions"
def convert_documents_batch(file_list, *, conversion_root=DEFAULT_CONVERSION_ROOT): ...
```

Copy current conversion function body and comments to document_conversion.py; only root becomes injectable for isolated tests. Dashboard imports convert_documents_batch under the same name, retaining existing handler mocks. Preserve proot-distro/LibreOffice arguments, private profile per batch, timeout/killpg cleanup, filename sanitization, PDF-vs-ZIP choice, duplicate filename suffixes and current partial-output behavior. No real converter launch in tests.

- [ ] Establish characterization tests against existing public routes/storage/conversion behavior before extraction. Existing integrated baseline is41Python tests plus passing actual-page browser suites.
- [ ] Extract NotesStore and keep dashboard wrapper signatures. Verify two-user isolation, Unicode roundtrip, anonymous/empty behavior, concurrent writes through one store,600 permissions, corrupted-read behavior, failed replacement preserves prior file and cleans temp file.
- [ ] Extract conversion into a side-effect-free importable module. Tests inject temporary conversion_root and fake Popen; assert one output PDF, multiple outputs ZIP, duplicate names match existing suffix policy, no-output error, timeout cleanup/killpg mocked, temporary batch directories removed. Never touch real Termux directories or launch LibreOffice/proot.
- [ ] Add HTTP notes load/save/role-isolation proof using existing disposable dashboard fixture without weakening conversion/auth tests.
- [ ] Run focused new tests, full Python suite once, then current actual-page browser suite once because public utility bindings moved. No Go test rerun. Run git diff --check and commit focused extraction/tests/docs.
- [ ] Write ignored .superpowers/sdd/utility-module-mappings.json declaring source app/notes.py and app/document_conversion.py to phone home/.local/lib/mobile-server corresponding filenames. Do not edit historical deployment baseline or collide with packaging worker's config edits. Root will reconcile new source mappings into validated release tooling before any deployment. Clearly report this integration prerequisite.

Acceptance: pure modules can be tested/imported without dashboard runtime secrets/telemetry, existing HTTP contracts and notes format unchanged, actual-page utility checks pass, no extra runtime dependency, and all new module release mappings explicitly reported.
