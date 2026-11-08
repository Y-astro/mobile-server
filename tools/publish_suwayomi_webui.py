#!/usr/bin/env python3
"""Bounded publication tool for Suwayomi WebUI.

Implements safe, atomic frontend publication and rollback:
- Validates WebUI.provenance.json and WebUI.zip contents before any mutation.
- Refuses path traversal, absolute paths, symlinks, duplicates, size abuse, and digest mismatch.
- Updates ONLY server.webUIFlavor to CUSTOM in server.conf, preserving all other bytes.
- Preserves prior hashed assets from the existing live WebUI for in-flight tabs.
- Uses atomic directory exchange (renameat2 RENAME_EXCHANGE) or staged backup.
- Durable journal with rollback and recovery capability.
- Uses TermuxLifecycleDriver lock across stop/swap/start/health to prevent races.
- Verifies live output manifest and healthy backend before success.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("publish_suwayomi_webui")

RENAME_EXCHANGE = 2
AT_FDCWD = -100

MAX_ZIP_ENTRIES = 2000
MAX_UNCOMPRESSED_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MiB limit for safety
MAX_ENTRY_SIZE = 20 * 1024 * 1024


class PublicationError(Exception):
    """Base error for WebUI publication failures."""


def _get_libc():
    try:
        return ctypes.CDLL(None)
    except Exception:
        return None


def test_rename_exchange_support(test_dir: Path) -> bool:
    """Feature-test renameat2 RENAME_EXCHANGE support before mutation."""
    d1 = test_dir / ".test_exchange_a"
    d2 = test_dir / ".test_exchange_b"
    try:
        d1.mkdir(exist_ok=True)
        d2.mkdir(exist_ok=True)
        (d1 / "a.txt").write_text("a")
        (d2 / "b.txt").write_text("b")
        _rename_exchange(d1, d2)
        if (d1 / "b.txt").exists() and (d2 / "a.txt").exists():
            return True
        return False
    except OSError:
        return False
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)


def _rename_exchange(src: Path, dst: Path) -> None:
    """Atomically exchange two paths using renameat2(RENAME_EXCHANGE)."""
    libc = _get_libc()
    if libc is None:
        raise OSError(errno.ENOSYS, "C library not available for renameat2")

    src_bytes = os.fsencode(str(src))
    dst_bytes = os.fsencode(str(dst))
    dir_fd = AT_FDCWD

    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        ret = renameat2(dir_fd, src_bytes, dir_fd, dst_bytes, RENAME_EXCHANGE)
        if ret == 0:
            return
        err = ctypes.get_errno()
        raise OSError(err, f"renameat2 failed: {os.strerror(err)}")

    syscall = getattr(libc, "syscall", None)
    if syscall is None:
        raise OSError(errno.ENOSYS, "renameat2 and syscall not found in libc")

    machine = platform.machine().lower()
    sys_renameat2 = None
    if "x86_64" in machine or "amd64" in machine:
        sys_renameat2 = 316
    elif "aarch64" in machine or "arm64" in machine:
        sys_renameat2 = 276
    elif "arm" in machine:
        sys_renameat2 = 382
    elif "i386" in machine or "i686" in machine:
        sys_renameat2 = 353
    elif "riscv64" in machine:
        sys_renameat2 = 276

    if sys_renameat2 is None:
        raise OSError(errno.ENOSYS, f"SYS_renameat2 syscall unknown for architecture {machine}")

    ret = syscall(sys_renameat2, dir_fd, src_bytes, dir_fd, dst_bytes, RENAME_EXCHANGE)
    if ret == 0:
        return
    err = ctypes.get_errno()
    raise OSError(err, f"renameat2 syscall failed: {os.strerror(err)}")


def compute_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_tree_manifest(directory: Path) -> Dict[str, str]:
    manifest: Dict[str, str] = {}
    if not directory.exists():
        return manifest
    for root, _, files in os.walk(directory):
        for f in sorted(files):
            full_path = Path(root) / f
            rel = str(full_path.relative_to(directory)).replace(os.sep, "/")
            manifest[rel] = compute_file_sha256(full_path)
    return manifest


def validate_provenance_and_zip(
    zip_path: Path,
    prov_path: Path,
    expected_zip_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    if not zip_path.is_file() or not prov_path.is_file():
        raise PublicationError(f"Missing archive or provenance file: {zip_path}, {prov_path}")

    actual_zip_sha = compute_file_sha256(zip_path)
    if expected_zip_sha256 and actual_zip_sha != expected_zip_sha256:
        raise PublicationError(
            f"Archive digest mismatch: expected {expected_zip_sha256}, got {actual_zip_sha}"
        )

    try:
        prov = json.loads(prov_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise PublicationError(f"Failed to parse provenance JSON: {e}") from e

    if not isinstance(prov, dict):
        raise PublicationError("Provenance payload is not a JSON object")

    prov_zip_sha = prov.get("archive_sha256")
    if prov_zip_sha and prov_zip_sha != actual_zip_sha:
        raise PublicationError(
            f"Provenance archive_sha256 {prov_zip_sha} does not match archive file {actual_zip_sha}"
        )

    files_manifest = prov.get("files")
    if not isinstance(files_manifest, dict) or not files_manifest:
        raise PublicationError("Provenance missing non-empty 'files' mapping")

    # Inspect ZIP without uncompressing
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infolist = zf.infolist()
            if len(infolist) > MAX_ZIP_ENTRIES:
                raise PublicationError(f"Archive entry count {len(infolist)} exceeds {MAX_ZIP_ENTRIES}")

            seen_paths: Set[str] = set()
            total_uncompressed = 0

            for info in infolist:
                # Disallow absolute paths and traversal
                clean_name = info.filename.replace("\\", "/")
                if clean_name.startswith("/") or clean_name.startswith("../") or "/../" in clean_name:
                    raise PublicationError(f"Malicious or illegal path in archive: {info.filename}")

                # Reject symlinks in external attributes
                # Unix mode stored in upper 16 bits of external_attr
                unix_mode = info.external_attr >> 16
                if unix_mode and (unix_mode & 0o170000 == 0o120000):
                    raise PublicationError(f"Archive contains prohibited symlink entry: {info.filename}")

                if clean_name in seen_paths:
                    raise PublicationError(f"Duplicate path in archive: {clean_name}")
                seen_paths.add(clean_name)

                if info.file_size > MAX_ENTRY_SIZE:
                    raise PublicationError(f"Entry {clean_name} size {info.file_size} exceeds {MAX_ENTRY_SIZE}")
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_TOTAL_BYTES:
                    raise PublicationError(f"Total uncompressed size exceeds limit: {total_uncompressed}")

                if not info.is_dir():
                    norm_rel = clean_name.lstrip("./")
                    if norm_rel not in files_manifest:
                        raise PublicationError(f"Archive contains unmanifested file: {norm_rel}")

            for manifest_rel in files_manifest:
                if manifest_rel not in seen_paths and f"./{manifest_rel}" not in seen_paths:
                    raise PublicationError(f"Manifest entry missing from archive: {manifest_rel}")

    except zipfile.BadZipFile as e:
        raise PublicationError(f"Invalid ZIP file: {e}") from e

    return prov


def extract_and_verify_candidate(
    zip_path: Path,
    expected_manifest: Dict[str, str],
    target_extract_dir: Path,
) -> Dict[str, str]:
    if target_extract_dir.exists():
        shutil.rmtree(target_extract_dir)
    target_extract_dir.mkdir(parents=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            norm_name = info.filename.replace("\\", "/").lstrip("./")
            dest = target_extract_dir / norm_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)

    actual_manifest = compute_tree_manifest(target_extract_dir)
    for rel, exp_sha in expected_manifest.items():
        act_sha = actual_manifest.get(rel)
        if not act_sha:
            raise PublicationError(f"Extracted candidate missing {rel}")
        if act_sha != exp_sha:
            raise PublicationError(f"Extracted digest mismatch on {rel}: expected {exp_sha}, got {act_sha}")

    for act_rel in actual_manifest:
        if act_rel not in expected_manifest:
            raise PublicationError(f"Extracted candidate contains unexpected file {act_rel}")

    return actual_manifest


def preserve_prior_assets(
    live_webui_dir: Path,
    candidate_dir: Path,
    candidate_manifest: Dict[str, str],
) -> Dict[str, str]:
    """Preserve prior hashed lazy assets from live into candidate if absent.
    
    In-flight tabs rely on existing chunk URLs (e.g. assets/*.js, *.css).
    Never overwrite index.html, sw.js, or any file present in candidate.
    """
    final_manifest = dict(candidate_manifest)
    if not live_webui_dir.exists():
        return final_manifest

    prior_manifest = compute_tree_manifest(live_webui_dir)
    for rel, sha in prior_manifest.items():
        if rel in candidate_manifest:
            continue
        # Protected entry points that must NEVER be retained from old tree
        if rel in ("index.html", "sw.js", "service-worker.js", "manifest.json", "favicon.ico"):
            continue
        # Only copy hashed/static asset chunks (e.g. assets/*)
        if rel.startswith("assets/") or rel.startswith("static/") or re.search(r"-[a-f0-9]{8,}\.", rel):
            src_file = live_webui_dir / rel
            dst_file = candidate_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            final_manifest[rel] = sha

    return final_manifest


def modify_server_conf_flavor_custom(conf_content: str) -> Tuple[str, bool]:
    """Change ONLY server.webUIFlavor to CUSTOM in server.conf.
    
    Preserves all other lines, comments, formatting, and bytes.
    Never prints or logs configuration contents.
    """
    pattern = re.compile(r'^(?P<prefix>\s*server\.webUIFlavor\s*=\s*)(?P<val>("[^"]*"|\S+))(?P<suffix>.*)$', re.MULTILINE)
    m = pattern.search(conf_content)
    if not m:
        # If not present, append minimal configuration line
        new_content = conf_content + ("\n" if not conf_content.endswith("\n") else "") + 'server.webUIFlavor = "CUSTOM"\n'
        return new_content, True

    prefix = m.group("prefix")
    old_val = m.group("val")
    suffix = m.group("suffix")

    if old_val == '"CUSTOM"' or old_val == "CUSTOM":
        return conf_content, False

    new_line = f'{prefix}"CUSTOM"{suffix}'
    start, end = m.span()
    new_content = conf_content[:start] + new_line + conf_content[end:]
    return new_content, True


@dataclass
class PublicationJournal:
    action: str
    live_webui_dir: str
    backup_webui_dir: str
    candidate_webui_dir: str
    conf_path: str
    conf_backup_path: str
    prior_tree_hashes: Dict[str, str]
    expected_final_hashes: Dict[str, str]
    prior_conf_sha: str
    target_conf_sha: str
    exchange_method: str
    status: str

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Optional[PublicationJournal]:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


class WebUIPublisher:
    def __init__(
        self,
        home_dir: Path,
        driver: Any = None,
        staging_dir: Optional[Path] = None,
    ):
        self.home_dir = home_dir.resolve()
        self.live_webui_dir = self.home_dir / "suwayomi" / "data" / "webUI"
        self.conf_path = self.home_dir / "suwayomi" / "data" / "server.conf"
        self.staging_dir = (staging_dir or (self.home_dir / "suwayomi" / "data" / ".webui_staging")).resolve()
        self.journal_path = self.staging_dir / "publication_journal.json"
        self.driver = driver

    def _ensure_driver(self):
        if self.driver is None:
            from scripts.termux_activation import TermuxLifecycleDriver
            self.driver = TermuxLifecycleDriver()
        return self.driver

    def check_service_intent(self) -> None:
        driver = self._ensure_driver()
        # Check paused
        is_paused_fn = getattr(driver, "is_paused", None)
        if callable(is_paused_fn) and is_paused_fn():
            raise PublicationError("Refusing publication: system is in paused state")

        # Check disabled services
        disabled_dir = getattr(driver, "disabled_dir", self.home_dir / ".config" / "services_disabled")
        if (disabled_dir / "suwayomi").exists() or (disabled_dir / "suwayomi.disabled").exists():
            raise PublicationError("Refusing publication: suwayomi is disabled")

        # Check registered services
        services = driver.services() if callable(getattr(driver, "services", None)) else {}
        if services and "suwayomi" not in services:
            raise PublicationError("Refusing publication: suwayomi service not defined in catalog")

    def publish(
        self,
        zip_path: Path,
        prov_path: Path,
        expected_zip_sha256: Optional[str] = None,
        expected_prior_tree_sha: Optional[Dict[str, str]] = None,
        expected_prior_conf_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute full validated publication."""
        # 1. Scope and safe path validation
        for p in (self.live_webui_dir, self.conf_path, self.staging_dir):
            if p.is_symlink():
                raise PublicationError(f"Prohibited symlink encountered at {p}")

        # Ensure staging dir under same filesystem as live_webui_dir parent
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        # 2. Refuse if suwayomi paused or disabled
        self.check_service_intent()

        # 3. Validate ZIP and Provenance strictly BEFORE any mutations
        prov = validate_provenance_and_zip(zip_path, prov_path, expected_zip_sha256)
        files_manifest = prov["files"]

        # 4. Validate prior live tree and conf if expectations provided
        prior_tree_hashes = compute_tree_manifest(self.live_webui_dir)
        if expected_prior_tree_sha is not None:
            if prior_tree_hashes != expected_prior_tree_sha:
                raise PublicationError("Prior live WebUI tree hashes do not match expected manifest (drift detected)")

        if not self.conf_path.is_file():
            raise PublicationError(f"server.conf not found at {self.conf_path}")

        prior_conf_bytes = self.conf_path.read_bytes()
        prior_conf_sha = hashlib.sha256(prior_conf_bytes).hexdigest()
        if expected_prior_conf_sha is not None and prior_conf_sha != expected_prior_conf_sha:
            raise PublicationError(f"server.conf hash {prior_conf_sha} != expected {expected_prior_conf_sha} (drift detected)")

        # 5. Extract candidate into staging and verify
        candidate_extract_dir = self.staging_dir / "candidate_webui"
        extracted_manifest = extract_and_verify_candidate(zip_path, files_manifest, candidate_extract_dir)

        # 6. Preserve old hashed assets
        final_manifest = preserve_prior_assets(self.live_webui_dir, candidate_extract_dir, extracted_manifest)

        # 7. Compute server.conf modification
        prior_conf_str = prior_conf_bytes.decode("utf-8")
        new_conf_str, conf_changed = modify_server_conf_flavor_custom(prior_conf_str)
        target_conf_bytes = new_conf_str.encode("utf-8")
        target_conf_sha = hashlib.sha256(target_conf_bytes).hexdigest()

        # 8. Feature test atomic exchange before stopping service
        can_exchange = test_rename_exchange_support(self.staging_dir)
        exchange_method = "renameat2_exchange" if can_exchange else "two_step_staged"

        # Prepare backup paths in staging
        backup_webui_dir = self.staging_dir / "backup_webui"
        conf_backup_file = self.staging_dir / "server.conf.backup"
        conf_backup_file.write_bytes(prior_conf_bytes)

        if backup_webui_dir.exists():
            shutil.rmtree(backup_webui_dir)

        # 9. Record durable journal before mutation
        journal = PublicationJournal(
            action="publish",
            live_webui_dir=str(self.live_webui_dir),
            backup_webui_dir=str(backup_webui_dir),
            candidate_webui_dir=str(candidate_extract_dir),
            conf_path=str(self.conf_path),
            conf_backup_path=str(conf_backup_file),
            prior_tree_hashes=prior_tree_hashes,
            expected_final_hashes=final_manifest,
            prior_conf_sha=prior_conf_sha,
            target_conf_sha=target_conf_sha,
            exchange_method=exchange_method,
            status="prepared",
        )
        journal.save(self.journal_path)

        driver = self._ensure_driver()

        # 10. Lock services across stop, swap, start, health
        lock_ctx = getattr(driver, "lock", None)
        lock_mgr = lock_ctx("services", timeout=10) if callable(lock_ctx) else None

        class _DummyLock:
            def __enter__(self): return self
            def __exit__(self, *args): pass

        mgr = lock_mgr if lock_mgr is not None else _DummyLock()

        with mgr:
            # Snapshot unrelated process identities before stop
            before_procs = {}
            if callable(getattr(driver, "matching_processes", None)):
                for sid in ("antra", "navidrome", "caddy", "suwayomi_scheduler"):
                    try:
                        before_procs[sid] = driver.matching_processes(sid)
                    except Exception:
                        pass

            journal.status = "stopping"
            journal.save(self.journal_path)

            # Terminate only suwayomi
            suwayomi_procs = driver.matching_processes("suwayomi") if callable(getattr(driver, "matching_processes", None)) else []
            if suwayomi_procs and callable(getattr(driver, "terminate", None)):
                driver.terminate(suwayomi_procs)

            journal.status = "swapping"
            journal.save(self.journal_path)

            try:
                # Perform directory swap
                if exchange_method == "renameat2_exchange":
                    if not self.live_webui_dir.exists():
                        self.live_webui_dir.mkdir(parents=True)
                    _rename_exchange(candidate_extract_dir, self.live_webui_dir)
                    # Now candidate_extract_dir holds the prior live tree! Move it to backup
                    candidate_extract_dir.replace(backup_webui_dir)
                else:
                    if self.live_webui_dir.exists():
                        self.live_webui_dir.replace(backup_webui_dir)
                    candidate_extract_dir.replace(self.live_webui_dir)

                # Update server.conf atomically
                if conf_changed:
                    tmp_conf = self.conf_path.with_suffix(".tmp")
                    tmp_conf.write_bytes(target_conf_bytes)
                    tmp_conf.replace(self.conf_path)

                journal.status = "starting"
                journal.save(self.journal_path)

                # Launch suwayomi
                services = driver.services() if callable(getattr(driver, "services", None)) else {}
                suwayomi_cfg = services.get("suwayomi", {})
                if callable(getattr(driver, "launch", None)):
                    driver.launch("suwayomi", suwayomi_cfg)

                # Health check
                is_healthy = False
                if callable(getattr(driver, "healthy", None)):
                    is_healthy = driver.healthy("suwayomi", suwayomi_cfg)
                else:
                    is_healthy = True

                if not is_healthy:
                    raise PublicationError("Suwayomi backend reported unhealthy after publication")

                # Verify live manifest
                live_manifest = compute_tree_manifest(self.live_webui_dir)
                if live_manifest != final_manifest:
                    raise PublicationError("Live tree manifest does not match expected final manifest")

                # Check unrelated process identities did not mutate
                if callable(getattr(driver, "matching_processes", None)):
                    for sid, procs in before_procs.items():
                        current = driver.matching_processes(sid)
                        if procs != current:
                            logger.warning(f"Process identity change detected for {sid}: was {procs}, now {current}")

                journal.status = "committed"
                journal.save(self.journal_path)

            except Exception as e:
                logger.error(f"Publication failed: {e}. Initiating rollback...")
                self._execute_rollback(journal, driver)
                raise

        return {
            "status": "success",
            "manifest_count": len(final_manifest),
            "conf_sha256": target_conf_sha,
            "exchange_method": exchange_method,
        }

    def _execute_rollback(self, journal: PublicationJournal, driver: Any) -> None:
        """Roll back live directory and server.conf to prior state."""
        journal.status = "rolling_back"
        journal.save(self.journal_path)

        # Terminate any running suwayomi
        suwayomi_procs = driver.matching_processes("suwayomi") if callable(getattr(driver, "matching_processes", None)) else []
        if suwayomi_procs and callable(getattr(driver, "terminate", None)):
            driver.terminate(suwayomi_procs)

        backup_webui = Path(journal.backup_webui_dir)
        live_webui = Path(journal.live_webui_dir)
        conf_backup = Path(journal.conf_backup_path)
        conf_live = Path(journal.conf_path)

        # Restore config
        if conf_backup.is_file():
            conf_backup.replace(conf_live)

        # Restore webui tree
        if backup_webui.exists():
            if live_webui.exists():
                shutil.rmtree(live_webui)
            backup_webui.replace(live_webui)

        # Restart backend
        services = driver.services() if callable(getattr(driver, "services", None)) else {}
        suwayomi_cfg = services.get("suwayomi", {})
        if callable(getattr(driver, "launch", None)):
            driver.launch("suwayomi", suwayomi_cfg)

        journal.status = "rolled_back"
        journal.save(self.journal_path)

    def recover(self) -> Dict[str, Any]:
        """Interrupted state recovery using durable journal."""
        if not self.journal_path.is_file():
            return {"status": "no_journal_found"}

        journal = PublicationJournal.load(self.journal_path)
        if not journal:
            raise PublicationError("Invalid journal format")

        if journal.status in ("committed", "rolled_back"):
            return {"status": "already_consistent", "journal_status": journal.status}

        driver = self._ensure_driver()
        lock_ctx = getattr(driver, "lock", None)
        lock_mgr = lock_ctx("services", timeout=10) if callable(lock_ctx) else None

        class _DummyLock:
            def __enter__(self): return self
            def __exit__(self, *args): pass

        prev_status = journal.status
        with (lock_mgr or _DummyLock()):
            self._execute_rollback(journal, driver)

        return {"status": "recovered", "previous_status": prev_status}


def main():
    parser = argparse.ArgumentParser(description="Suwayomi WebUI publication tool")
    parser.add_argument("--home-dir", type=Path, default=Path(os.environ.get("HOME", "/data/data/com.termux/files/home")))
    parser.add_argument("--zip", type=Path, required=True, help="Path to WebUI.zip")
    parser.add_argument("--provenance", type=Path, required=True, help="Path to WebUI.provenance.json")
    parser.add_argument("--expected-sha256", type=str, default=None, help="Expected SHA256 of WebUI.zip")
    parser.add_argument("--recover", action="store_true", help="Recover from interrupted state")

    args = parser.parse_args()

    publisher = WebUIPublisher(home_dir=args.home_dir)
    if args.recover:
        res = publisher.recover()
        print(json.dumps(res))
        return

    res = publisher.publish(
        zip_path=args.zip,
        prov_path=args.provenance,
        expected_zip_sha256=args.expected_sha256,
    )
    print(json.dumps(res))


if __name__ == "__main__":
    main()
