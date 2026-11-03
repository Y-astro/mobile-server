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
    print(formatted, flush=True)
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
                ok, msg = trigger_update(retries=10, delay=15)
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
    args = parser.parse_args()

    if args.now:
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
