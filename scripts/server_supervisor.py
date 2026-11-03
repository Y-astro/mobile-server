#!/data/data/com.termux/files/usr/bin/python3
"""Termux service recovery. Run --ensure at boot; --daemon monitors health.

Only standard-library dependencies. Android JobScheduler runs the boot entry
independently; this process cannot prevent Android from killing the whole UID.
"""

import os
try:
    with open("/data/data/com.termux/files/home/parent_pid.txt", "a") as _pf:
        _pf.write(f"invoked: pid={os.getpid()} ppid={os.getppid()} args={os.sys.argv}\n")
except Exception:
    pass
import argparse
import contextlib
import fcntl
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import time

HOME = Path('/data/data/com.termux/files/home')
PREFIX = Path('/data/data/com.termux/files/usr')
MOBILE_LIB = Path.home() / '.local/lib/mobile-server'
_lib_dir = MOBILE_LIB if MOBILE_LIB.is_dir() else (HOME / '.local/lib/mobile-server' if (HOME / '.local/lib/mobile-server').is_dir() else Path(__file__).resolve().parents[1] / 'app')
if str(_lib_dir) not in sys.path:
    sys.path.insert(0, str(_lib_dir))

from service_catalog import runtime_services
STATE = HOME / '.config/server_recovery'
DISABLED = HOME / '.config/services_disabled'
PAUSED = STATE / 'paused'
PUBLIC_PAUSED = Path('/storage/emulated/0/.phone-server-paused')
LOG = logging.getLogger('server-recovery')
INTERVAL = 10
FAILURES = 3


def services():
    return runtime_services(str(HOME), str(PREFIX))


def initialize():
    STATE.mkdir(parents=True, exist_ok=True)
    DISABLED.mkdir(parents=True, exist_ok=True)
    (HOME / '.cache/filebrowser').mkdir(parents=True, exist_ok=True)
    (HOME / '.config/battery_guard').mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(STATE / 'supervisor.log', maxBytes=1024*1024, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


@contextlib.contextmanager
def lock(name, blocking=True, timeout=None):
    with (STATE / (name + '.lock')).open('a') as f:
        if timeout is None:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Service control busy; retry shortly')
                    time.sleep(.1)
        yield


def atomic_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def process_identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if stat[0] == 'Z':
            return None
        return stat[19]  # starttime, field 22
    except (OSError, IndexError):
        return None


def matching_processes(sid):
    """Match argv tokens, never a broad pkill or the invoking SSH command."""
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
            base = Path(args[0]).name
            if sid == 'ssh':
                match = base == 'sshd' and '-R' not in args
            elif sid == 'dashboard':
                match = base.startswith('python') and str(PREFIX / 'bin/dashboard') in args
            elif sid == 'caddy':
                match = base == 'caddy' and 'run' in args and str(HOME / '.config/caddy/Caddyfile') in args
            elif sid in ('terminal', 'agy', 'laptop_terminal'):
                port = str(services()[sid]['port'])
                match = base == 'ttyd' and any(args[i:i+2] == ['-p', port] for i in range(len(args)-1))
            elif sid == 'transfer_gateway':
                match = args[0] == str(HOME / 'bin/mobile-gateway')
            elif sid == 'battery_guard':
                match = base in ('bash', 'sh') and str(HOME / 'bin/battery_guard.sh') in args and '--daemon' in args
            elif sid == 'suwayomi_filter':
                match = base.startswith('python') and str(HOME / 'bin/suwayomi_graphql_filter.py') in args
            elif sid == 'suwayomi':
                match = (base == 'java' and any('suwayomi' in a.lower() for a in args)) or any('start_suwayomi' in a for a in args) or ('proot' in base and any('suwayomi' in a.lower() for a in args))
            else:
                owned_argument = {
                    'adguard': str(HOME / 'adguard/data/AdGuardHome.yaml'),
                    'filebrowser': str(HOME / '.config/filebrowser/filebrowser.db'),
                    'syncthing': str(HOME / '.local/state/syncthing'),
                    'vaultwarden': str(HOME / 'vaultwarden/vaultwarden'),
                    'tor': str(HOME / '.config/tor/torrc'),
                }[sid]
                match = base == {'adguard': 'AdGuardHome'}.get(sid, sid) and owned_argument in args
            if match:
                identity = process_identity(int(proc.name))
                if identity:
                    found.append((int(proc.name), identity))
        except (OSError, IndexError):
            continue
    return found


def terminate(records):
    for sig, deadline in ((signal.SIGTERM, 4), (signal.SIGKILL, 1)):
        for pid, ident in records:
            if process_identity(pid) == ident:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, sig)
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if not any(process_identity(pid) == ident for pid, ident in records):
                return
            time.sleep(.1)


def stop_agy():
    # PRoot can survive its tmux pane's SIGHUP. Capture only this runner's
    # processes/descendants before removing the pane, then stop them by identity.
    processes = {}
    owned = set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
            stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            pid = int(proc.name)
            processes[pid] = (int(stat[1]), process_identity(pid))
            if '/usr/local/bin/agy-runner-loop' in args and Path(args[0]).name in ('bash', 'proot'):
                owned.add(pid)
        except (OSError, IndexError, ValueError):
            continue
    while True:
        descendants = {pid for pid, (parent, _) in processes.items() if parent in owned}
        if descendants <= owned:
            break
        owned.update(descendants)
    try:
        res = subprocess.run([str(PREFIX / 'bin/tmux'), 'list-sessions', '-F', '#{session_name}'], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            for s in res.stdout.splitlines():
                s = s.strip()
                if s == 'agy' or s.startswith('agy-'):
                    subprocess.run([str(PREFIX / 'bin/tmux'), 'kill-session', '-t', f'={s}'], timeout=2, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        LOG.warning('Unable to close AGY tmux session; stopping captured processes')
    terminate([(pid, processes[pid][1]) for pid in owned if processes[pid][1] is not None])


def healthy(sid, cfg):
    if not cfg.get('port'):
        status = HOME / '.config/battery_guard/status.json'
        try:
            return bool(matching_processes(sid)) and time.time() - status.stat().st_mtime < 360
        except OSError:
            return False
    try:
        if 'path' in cfg:
            conn = http.client.HTTPConnection('127.0.0.1', cfg['port'], timeout=2)
            try:
                conn.request('GET', cfg['path'], headers={'Connection': 'close'})
                response = conn.getresponse()
                # A working gateway can return 502 while its backend recovers.
                # Dashboard health is checked separately; do not cascade restarts.
                return response.status in (200, 204, 301, 302, 303, 307, 308, 401, 403) or (sid == 'caddy' and response.status in (502, 503, 504))
            finally:
                conn.close()
        with socket.create_connection(('127.0.0.1', cfg['port']), timeout=2) as sock:
            sock.settimeout(2)
            if cfg.get('protocol') == 'socks5':
                sock.sendall(b'\x05\x01\x00')
                return sock.recv(2) == b'\x05\x00'
            return sock.recv(4) == b'SSH-'
    except (OSError, http.client.HTTPException):
        return False


def get_tracer_pid():
    try:
        with open(f"/proc/{os.getpid()}/status") as fh:
            for line in fh:
                if line.startswith("TracerPid:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def launch(sid, cfg):
    log_path = STATE / f'{sid}.log'
    # Rotate before restart; do not truncate a running writer's file.
    if log_path.exists() and log_path.stat().st_size > 2*1024*1024:
        log_path.replace(log_path.with_suffix('.log.1'))
    tpid = get_tracer_pid()
    if tpid != 0:
        LOG.warning('Refusing to launch %s directly from traced/PRoot environment (TracerPid=%d); daemon will start it cleanly', sid, tpid)
        return None
    env = os.environ.copy()
    for k in ('container', 'PD_CONTAINER', 'PD_IMAGE', 'PD_IMAGE_ID', 'PROOT_L2S_DIR', 'ANTIGRAVITY_AGENT', 'ANTIGRAVITY_CONVERSATION_ID', 'ANTIGRAVITY_TRAJECTORY_ID'):
        env.pop(k, None)
    env['USER'] = 'u0_a505'
    env['TMUX_TMPDIR'] = str(PREFIX / 'var/run/tmux-10505')
    env.update(HOME=str(HOME), PREFIX=str(PREFIX), TMPDIR=str(PREFIX / 'tmp'), PATH=f'{PREFIX}/bin:{PREFIX}/bin/applets:' + env.get('PATH', ''), GOMEMLIMIT='256MiB', GOGC='100')
    with log_path.open('ab', buffering=0) as out:
        proc = subprocess.Popen(cfg['cmd'], env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True)
    LOG.info('START %s pid=%s', sid, proc.pid)
    return proc


class Supervisor:
    def __init__(self):
        self.history = {}
        self.children = []

    def cycle(self, initial=False):
        report = {}
        self.children = [p for p in self.children if p.poll() is None]
        for sid, cfg in services().items():
            logfile = STATE / f'{sid}.log'
            if logfile.exists() and logfile.stat().st_size > 2*1024*1024:
                shutil.copyfile(logfile, logfile.with_suffix('.log.1'))
                # Keep existing process descriptors writing into the current file.
                with logfile.open('r+b') as out:
                    out.truncate(0)
            if (DISABLED / sid).exists() or (PAUSED.exists() and sid != 'ssh'):
                report[sid] = {'state': 'disabled'}
                continue
            entry = self.history.setdefault(sid, dict(failures=0, retries=0, next_start=0))
            ok = healthy(sid, cfg)
            if ok:
                entry['failures'] = 0
                if time.monotonic() - entry.get('started', 0) > 120:
                    entry['retries'] = 0
                report[sid] = {'state': 'healthy'}
                continue
            entry['failures'] += 1
            records = matching_processes(sid)
            due = time.monotonic() >= entry['next_start']
            if due and ((initial and not records) or (not initial and entry['failures'] >= FAILURES)):
                LOG.warning('RECOVER %s failures=%s old_pids=%s', sid, entry['failures'], [p for p, _ in records])
                terminate(records)
                try:
                    p = launch(sid, cfg)
                    if p is not None:
                        self.children.append(p)
                except OSError:
                    LOG.exception('Unable to launch %s', sid)
                entry['retries'] += 1
                entry['started'] = time.monotonic()
                entry['next_start'] = time.monotonic() + min(300, 10 * 2 ** min(entry['retries'], 5))
                entry['failures'] = 0
            report[sid] = {'state': 'recovering', 'failures': entry['failures'], 'retries': entry['retries']}
        atomic_json(STATE / 'status.json', dict(timestamp=time.time(), pid=os.getpid(), paused=PAUSED.exists(), services=report))
        with contextlib.suppress(OSError):
            Path('/storage/emulated/0/.phone-server-heartbeat').write_text(str(int(time.time())))


def ensure():
    with lock('services'):
        Supervisor().cycle(initial=True)
    # Lifetime lock is held only by the daemon, never inherited by children.
    try:
        with lock('daemon', blocking=False):
            pass
    except BlockingIOError:
        return
    if get_tracer_pid() != 0:
        LOG.warning('Refusing to spawn daemon from inside traced/PRoot environment')
        return
    with (STATE / 'daemon-console.log').open('ab') as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--daemon'], stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True)


def control(sid, enabled):
    cfg = services()[sid]
    with lock('services', timeout=6):
        flag = DISABLED / sid
        if enabled:
            if PAUSED.exists():
                raise RuntimeError('Server is deliberately paused; run server-up first')
            flag.unlink(missing_ok=True)
            if not healthy(sid, cfg):
                terminate(matching_processes(sid))
                launch(sid, cfg)
        else:
            flag.touch()
            terminate(matching_processes(sid))
            if sid == 'agy':
                stop_agy()
    try:
        with lock('daemon', blocking=False):
            pass
        with open(f"/proc/{os.getpid()}/status") as fh:
            for line in fh:
                if line.startswith("TracerPid:"):
                    if int(line.split()[1]) == 0:
                        with (STATE / 'daemon-console.log').open('ab') as out:
                            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--daemon'], stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True)
                    break
    except (BlockingIOError, OSError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    for name in ('ensure', 'daemon', 'pause', 'resume', 'status'):
        action.add_argument('--' + name, action='store_true')
    action.add_argument('--service', choices=list(services()))
    parser.add_argument('--enable', choices=('true', 'false'))
    args = parser.parse_args()
    initialize()
    if args.status:
        status = json.loads((STATE / 'status.json').read_text()) if (STATE / 'status.json').exists() else {}
        age = time.time() - status.get('timestamp', 0)
        daemon_running = True
        try:
            with lock('daemon', blocking=False):
                daemon_running = False
        except BlockingIOError:
            pass
        status.update(supervisor_running=daemon_running, stale=age > 120 or not daemon_running, age_seconds=round(age, 1))
        print(json.dumps(status, indent=2))
    elif args.service:
        if args.enable is None:
            parser.error('--service requires --enable true|false')
        control(args.service, args.enable == 'true')
    elif args.pause:
        with lock('services'):
            PAUSED.touch()
            with contextlib.suppress(OSError):
                PUBLIC_PAUSED.touch()
            for sid in services():
                if sid != 'ssh':
                    terminate(matching_processes(sid))
            stop_agy()
            LOG.info('Server deliberately paused; SSH remains available')
    elif args.resume:
        with lock('services'):
            PAUSED.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                PUBLIC_PAUSED.unlink(missing_ok=True)
        ensure()
    elif args.ensure:
        ensure()
    else:
        try:
            with lock('daemon', blocking=False):
                running = True
                def stop(signum, frame):
                    nonlocal running
                    running = False
                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                LOG.info('Daemon started pid=%s', os.getpid())
                supervisor = Supervisor()
                while running:
                    try:
                        with lock('services'):
                            supervisor.cycle()
                    except Exception:
                        LOG.exception('Health cycle failed; will retry')
                    for _ in range(INTERVAL):
                        if not running:
                            break
                        time.sleep(1)
        except BlockingIOError:
            return


if __name__ == '__main__':
    main()
