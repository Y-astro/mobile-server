#!/data/data/com.termux/files/usr/bin/python3
"""
Suwayomi Daily 12:00 AM Reading List Auto-Updater & Auto-Downloader
Triggers Suwayomi GraphQL updateCategoryManga for the Reading category (ID 1)
at exactly 12:00 AM (00:00:00) every day in local time (IST).
"""

import sys
import os
import json
import time
import fcntl
import signal
import argparse
import datetime
import stat
import urllib.request
import urllib.error
from pathlib import Path

# Paths
BASE_DIR = Path("/data/data/com.termux/files/home/suwayomi/data")
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "daily_update.log"
STATUS_FILE = LOG_DIR / "scheduler_status.json"
PID_FILE = BASE_DIR / "scheduler.pid"

GRAPHQL_URL = "http://127.0.0.1:4567/suwayomi/api/graphql"
READING_CATEGORIES = [1]  # Category 1 = "Reading"

_pid_file_fd = None

def log(msg: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    try:
        print(formatted, flush=True)
    except BrokenPipeError:
        # A failed capture companion must not stop the update scheduler.
        pass
    if os.environ.get("SUWAYOMI_BOUNDED_LOGGING") == "1":
        # In bounded capture mode, stdout is captured by companion; omit separate file writes.
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception as e:
        print(f"Error writing log: {e}", file=sys.stderr)

def get_next_midnight():
    """Calculate the next 00:00:00 (12:00 AM) local time datetime."""
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight <= now:
        midnight += datetime.timedelta(days=1)
    return midnight

def get_daemon_pid():
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # Verify process is alive and is suwayomi_daily_update
            os.kill(pid, 0)
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="ignore")
                if "suwayomi_daily_update" in cmd:
                    return pid
        except (ValueError, OSError):
            pass
    return None

def save_status(status: str, message: str, last_run: datetime.datetime = None, daemon_pid: int = None):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        next_run = get_next_midnight()
        
        # Load existing status to preserve fields
        data = {}
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass

        data.update({
            "last_status": status,
            "last_message": message,
            "next_scheduled_run": next_run.strftime("%Y-%m-%d %H:%M:%S"),
            "target_categories": READING_CATEGORIES,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

        if last_run:
            data["last_run_time"] = last_run.strftime("%Y-%m-%d %H:%M:%S")
            data["last_run_date"] = last_run.strftime("%Y-%m-%d")

        active_pid = daemon_pid or get_daemon_pid()
        if active_pid:
            data["daemon_pid"] = active_pid

        STATUS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"Failed to save status JSON: {e}")

def trigger_update(retries=5, delay=10):
    """Call Suwayomi GraphQL to update categories."""
    query = {
        "query": f"mutation {{ updateCategoryManga(input: {{ categories: {json.dumps(READING_CATEGORIES)} }}) {{ clientMutationId }} }}"
    }
    payload = json.dumps(query).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(GRAPHQL_URL, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_body = resp.read().decode("utf-8")
                res_json = json.loads(res_body)
                if "errors" in res_json:
                    log(f"GraphQL returned errors: {res_json['errors']}")
                    return False, f"Errors: {res_json['errors']}"
                log(f"Successfully triggered update for categories {READING_CATEGORIES}")
                return True, f"Success: updated categories {READING_CATEGORIES}"
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            log(f"Attempt {attempt}/{retries} failed to reach Suwayomi server: {e}")
            if attempt < retries:
                time.sleep(delay)
            else:
                return False, f"Failed after {retries} attempts: {e}"

# Server-side catch-up is independent of a reader client remaining open.
READ_CLEANUP_PAGE_SIZE = 50
PROTECTED_READ_CATEGORY = "currently reading"


def cleanup_graphql(query, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    request = urllib.request.Request(GRAPHQL_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("errors") or not isinstance(result.get("data"), dict):
        raise RuntimeError("Read-download cleanup GraphQL request failed")
    return result["data"]


def read_cleanup_policy(request=cleanup_graphql):
    data = request("""query ReadDownloadCleanupPolicy {
      categories { nodes { id name } }
      metas { nodes { key value } }
    }""")
    categories = data["categories"]["nodes"]
    protected = {row["id"] for row in categories
        if row["name"].strip().casefold() == PROTECTED_READ_CATEGORY}
    if len(protected) != 1 or any(type(value) is not int for value in protected):
        raise RuntimeError("Currently Reading category cannot be identified safely")
    meta = {row["key"]: row["value"] for row in data["metas"]["nodes"]}
    return protected, meta.get("webUI_deleteChaptersWithBookmark") == "true"


def eligible_read_download(row, protected, allow_bookmarked, *, downloaded=True):
    if type(row.get("id")) is not int or row["id"] <= 0:
        raise RuntimeError("Invalid cleanup chapter ID")
    user = row
    manga = row["manga"]
    categories = manga["categories"]["nodes"]
    if any(type(value.get("id")) is not int for value in categories):
        raise RuntimeError("Invalid cleanup category data")
    return (user.get("isRead") is True and user.get("isDownloaded") is downloaded
        and type(user.get("isBookmarked")) is bool
        and (allow_bookmarked or not user["isBookmarked"])
        and manga.get("inLibrary") is True
        and not protected.intersection(value["id"] for value in categories))


_READ_CLEANUP_QUERY = """query ReadDownloadCleanupCandidates($filter: ChapterFilterInput!, $first: Int!) {
  chapters(filter: $filter, first: $first, order: [{by: ID, byType: ASC}]) {
    nodes { id isRead isDownloaded isBookmarked
      manga { inLibrary categories { nodes { id } } } }
  }
}"""


def cleanup_read_downloads(*, dry_run=False, request=cleanup_graphql):
    # Keyset paging avoids skipping rows when deleting shrinks the result set.
    protected, allow_bookmarked = read_cleanup_policy(request)
    after = 0
    result = {"examined": 0, "eligible": 0, "deleted": 0, "protected": 0,
              "failed": 0, "dry_run": dry_run}
    base_filter = {"inLibrary": {"equalTo": True}, "isRead": {"equalTo": True},
                   "isDownloaded": {"equalTo": True}}
    while True:
        rows = request(_READ_CLEANUP_QUERY, {"filter": {
            **base_filter, "id": {"greaterThan": after}},
            "first": READ_CLEANUP_PAGE_SIZE})["chapters"]["nodes"]
        if not rows:
            break
        ids = [row["id"] for row in rows]
        if any(type(value) is not int or value <= after for value in ids) or ids != sorted(set(ids)):
            raise RuntimeError("Cleanup response does not advance chapter IDs")
        after = ids[-1]
        result["examined"] += len(rows)
        candidates = [row["id"] for row in rows
            if eligible_read_download(row, protected, allow_bookmarked)]
        result["protected"] += len(rows) - len(candidates)
        if not candidates:
            continue
        # Respect category moves, bookmark changes and mark-unread right before deletion.
        protected, allow_bookmarked = read_cleanup_policy(request)
        fresh = request(_READ_CLEANUP_QUERY, {"filter": {
            **base_filter, "id": {"in": candidates}},
            "first": READ_CLEANUP_PAGE_SIZE})["chapters"]["nodes"]
        eligible = sorted({row["id"] for row in fresh
            if row["id"] in candidates and eligible_read_download(row, protected, allow_bookmarked)})
        result["eligible"] += len(eligible)
        if dry_run or not eligible:
            continue
        try:
            deleted = request("""mutation CleanupReadDownloadedChapters($input: DeleteDownloadedChaptersInput!) {
              deleteDownloadedChapters(input: $input) { chapters { id isDownloaded } }
            }""", {"input": {"ids": eligible}})["deleteDownloadedChapters"]["chapters"]
            confirmed = {row["id"] for row in deleted
                if row["id"] in eligible and row.get("isDownloaded") is False}
            result["deleted"] += len(confirmed)
            result["failed"] += len(eligible) - len(confirmed)
            log(f"Read-download cleanup batch: deleted={sorted(confirmed)} failed={len(eligible) - len(confirmed)}")
        except Exception as error:
            result["failed"] += len(eligible)
            log(f"Read-download cleanup batch failed: {type(error).__name__}")
    log("Read-download cleanup: " + json.dumps(result, sort_keys=True))
    return result



# Both are existing download stores on this phone; do not scan unrelated storage.
READ_ARCHIVE_ROOTS = (BASE_DIR / "downloads/mangas",
                     Path("/storage/emulated/0/Suwayomi/downloads/mangas"))


def archive_filename(name):
    # Match the installed server's SafePath UTF-8 filename limits.
    name = name.strip(". ")
    if not name:
        return "(invalid)"
    name = "".join("_" if ord(c) < 32 or c in '\"*/:<>?\\|\x7f' else c for c in name)
    return name[:240].encode("utf-8")[:240].decode("utf-8", errors="ignore")


def archive_parts(manga, chapter):
    source = manga["source"]
    scanlator = chapter["scanlator"]
    return (archive_filename(source["name"] + " (" + source["lang"].upper() + ")"),
            archive_filename(manga["title"]),
            archive_filename((scanlator + "_" if scanlator is not None else "") + chapter["name"]) + ".cbz")


def cleanup_read_archives(*, dry_run=False, request=cleanup_graphql, roots=READ_ARCHIVE_ROOTS):
    result = {"deleted_archives": 0, "deleted_bytes": 0, "eligible_archives": 0,
              "unmatched_archives": 0, "failed": 0, "dry_run": dry_run}
    inventory = []
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for path in root.glob("*/*/*.cbz"):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and path.resolve().is_relative_to(root.resolve()):
                inventory.append((root, path, info))
    if not inventory:
        return result
    protected, allow_bookmarked = read_cleanup_policy(request)
    mangas = request("""query ReadArchiveLibrary {
      mangas(first: 10000, filter: {inLibrary: {equalTo: true}}) {
        totalCount nodes { id title source { name lang } }
      }
    }""")["mangas"]
    if mangas["totalCount"] != len(mangas["nodes"]):
        raise RuntimeError("Read archive library inventory is incomplete")
    by_directory = {}
    for manga in mangas["nodes"]:
        directory = archive_parts(manga, {"name": "", "scanlator": None})[:2]
        by_directory.setdefault(directory, []).append(manga)
    chapters_by_manga = {}
    candidates = {}
    for root, path, info in inventory:
        matches = by_directory.get(path.relative_to(root).parts[:2], [])
        if len(matches) != 1:
            result["unmatched_archives"] += 1
            continue
        manga = matches[0]
        mid = manga["id"]
        if mid not in chapters_by_manga:
            names = {}
            after = 0
            while True:
                rows = request("""query ReadArchiveChapterNames($filter: ChapterFilterInput!) {
                  chapters(first: 500, filter: $filter, order: [{by: ID, byType: ASC}]) {
                    nodes { id name scanlator }
                  }
                }""", {"filter": {"mangaId": {"equalTo": mid}, "id": {"greaterThan": after}}})["chapters"]["nodes"]
                if not rows:
                    break
                ids = [row["id"] for row in rows]
                if any(type(value) is not int or value <= after for value in ids) or ids != sorted(set(ids)):
                    raise RuntimeError("Read archive chapter inventory does not advance")
                after = ids[-1]
                for chapter in rows:
                    names.setdefault(archive_parts(manga, chapter), []).append(chapter["id"])
            chapters_by_manga[mid] = names
        ids = chapters_by_manga[mid].get(path.relative_to(root).parts, [])
        if len(ids) != 1:
            result["unmatched_archives"] += 1
            continue
        candidates.setdefault(ids[0], []).append((root, path, info))
    ids = sorted(candidates)
    for offset in range(0, len(ids), READ_CLEANUP_PAGE_SIZE):
        protected, allow_bookmarked = read_cleanup_policy(request)
        batch = ids[offset:offset + READ_CLEANUP_PAGE_SIZE]
        rows = request("""query ReadArchiveRecheck($filter: ChapterFilterInput!) {
          chapters(first: 50, filter: $filter) { nodes {
            id name scanlator isRead isDownloaded isBookmarked
            manga { inLibrary title source { name lang } categories { nodes { id } } }
          } }
        }""", {"filter": {"id": {"in": batch}}})["chapters"]["nodes"]
        for chapter in rows:
            cid = chapter["id"]
            if cid not in candidates or not eligible_read_download(chapter, protected, allow_bookmarked, downloaded=False):
                continue
            expected = archive_parts(chapter["manga"], chapter)
            for root, path, info in candidates[cid]:
                try:
                    current = path.lstat()
                    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                    if (identity(info) != identity(current) or not stat.S_ISREG(current.st_mode)
                            or not path.resolve().is_relative_to(root.resolve())
                            or path.relative_to(root).parts != expected):
                        continue
                    result["eligible_archives"] += 1
                    if not dry_run:
                        path.unlink()
                        result["deleted_archives"] += 1
                        result["deleted_bytes"] += info.st_size
                except FileNotFoundError:
                    continue
                except OSError as error:
                    result["failed"] += 1
                    log(f"Read archive cleanup failed for chapter {cid}: {type(error).__name__}")
    log("Read archive cleanup: " + json.dumps(result, sort_keys=True))
    return result


def nightly_update_and_cleanup():
    # Keep the midnight update first and keep cleanup failures from cancelling it.
    ok, message = trigger_update(retries=10, delay=15)
    try:
        result = cleanup_read_downloads()
        archives = cleanup_read_archives()
        message += (f"; read cleanup deleted={result['deleted']} failed={result['failed']}"
                    f"; stale archives deleted={archives['deleted_archives']} failed={archives['failed']}")
    except Exception as error:
        log(f"Read-download cleanup deferred after error: {type(error).__name__}")
        message += "; read cleanup failed (will retry next midnight)"
    return ok, message

def run_now():
    log("Manual update triggered...")
    now = datetime.datetime.now()
    ok, msg = trigger_update()
    save_status("success" if ok else "failure", msg, last_run=now)
    return ok

def daemonize():
    """Double-fork to fully detach daemon from terminal/session."""
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Fork #1 failed: {e}\n")
        sys.exit(1)

    os.setsid()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Fork #2 failed: {e}\n")
        sys.exit(1)

    sys.stdout.flush()
    sys.stderr.flush()

    with open("/dev/null", "rb", 0) as devnull_r:
        os.dup2(devnull_r.fileno(), sys.stdin.fileno())
    
    daemon_out = LOG_DIR / "scheduler_daemon.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_fd = os.open(daemon_out, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    os.dup2(out_fd, sys.stdout.fileno())
    os.dup2(out_fd, sys.stderr.fileno())

def run_daemon(foreground=False):
    global _pid_file_fd

    if not foreground:
        daemonize()

    # Enforce single instance via file lock
    _pid_file_fd = open(PID_FILE, "a+")
    try:
        fcntl.flock(_pid_file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another instance of suwayomi_daily_update daemon is already running. Exiting.")
        sys.exit(0)
    
    _pid_file_fd.seek(0)
    _pid_file_fd.truncate()
    _pid_file_fd.write(f"{os.getpid()}\n")
    _pid_file_fd.flush()

    log(f"Suwayomi Daily Scheduler daemon started (PID {os.getpid()}).")
    next_run = get_next_midnight()
    log(f"Target schedule: 12:00 AM everyday. Next run at: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    save_status("idle", f"Waiting for next scheduled run at {next_run.strftime('%Y-%m-%d %H:%M:%S')}", daemon_pid=os.getpid())

    last_run_date = None
    if STATUS_FILE.exists():
        try:
            st = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            last_run_date = st.get("last_run_date")
        except Exception:
            pass

    running = True
    def stop(signum, frame):
        nonlocal running
        log(f"Scheduler daemon received signal {signum}, stopping...")
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        try:
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # Trigger at 00:00 (12:00 AM) if not run today
            if now.hour == 0 and now.minute == 0 and last_run_date != today_str:
                log(f"12:00 AM reached for {today_str}! Starting daily reading update...")
                ok, msg = nightly_update_and_cleanup()
                last_run_date = today_str
                save_status("success" if ok else "failure", msg, last_run=now, daemon_pid=os.getpid())
                # Sleep 65s so we don't re-trigger in the same minute
                time.sleep(65)
                continue

            # Calculate seconds remaining until next midnight
            next_midnight = get_next_midnight()
            diff = (next_midnight - now).total_seconds()

            if diff > 60:
                time.sleep(30)
            else:
                time.sleep(max(0.5, min(diff, 1.0)))

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"Unexpected error in daemon loop: {e}")
            time.sleep(30)

    log("Scheduler daemon exiting cleanly.")
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def print_status():
    daemon_pid = get_daemon_pid()
    is_alive = daemon_pid is not None
    data = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error reading status: {e}")
            return
    data["daemon_is_running"] = is_alive
    if daemon_pid:
        data["daemon_pid"] = daemon_pid
    print(json.dumps(data, indent=2))

def main():
    parser = argparse.ArgumentParser(description="Suwayomi Daily Reading Update Scheduler")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--daemon", action="store_true", help="Run detached background daemon")
    group.add_argument("--foreground", action="store_true", help="Run daemon in foreground")
    group.add_argument("--now", action="store_true", help="Trigger update immediately")
    group.add_argument("--status", action="store_true", help="Show current scheduler status")
    group.add_argument("--cleanup-read-downloads", action="store_true", help="Clean read server downloads, except Currently Reading")
    parser.add_argument("--dry-run", action="store_true", help="Audit cleanup without deleting downloads")
    args = parser.parse_args()
    if args.dry_run and not args.cleanup_read_downloads:
        parser.error("--dry-run requires --cleanup-read-downloads")

    if args.cleanup_read_downloads:
        result = cleanup_read_downloads(dry_run=args.dry_run)
        archives = cleanup_read_archives(dry_run=args.dry_run)
        sys.exit(1 if result["failed"] or archives["failed"] else 0)
    elif args.now:
        success = run_now()
        sys.exit(0 if success else 1)
    elif args.status:
        print_status()
    elif args.daemon:
        run_daemon(foreground=False)
    elif args.foreground:
        run_daemon(foreground=True)

if __name__ == "__main__":
    main()
