#!/data/data/com.termux/files/usr/bin/python3
"""Termux service recovery. Run --ensure at boot; --daemon monitors health.

Only standard-library dependencies. Android JobScheduler runs the boot entry
independently; this process cannot prevent Android from killing the whole UID.
"""
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

HOME = Path(os.environ.get('HOME', '/data/data/com.termux/files/home'))
PREFIX = Path('/data/data/com.termux/files/usr')
STATE = HOME / '.config/server_recovery'
DISABLED = HOME / '.config/services_disabled'
PAUSED = STATE / 'paused'
PUBLIC_PAUSED = Path('/storage/emulated/0/.phone-server-paused')
LOG = logging.getLogger('server-recovery')
INTERVAL = 10
FAILURES = 3


def services():
    h, p = str(HOME), str(PREFIX)
    return {
        'ssh': dict(port=8022, cmd=[f'{p}/bin/sshd', '-D']),
        'dashboard': dict(port=8001, path='/healthz', cmd=[f'{p}/bin/python3', '-u', f'{p}/bin/dashboard']),
        'caddy': dict(port=8000, path='/healthz', cmd=[f'{p}/bin/caddy', 'run', '--config', f'{h}/.config/caddy/Caddyfile']),
        'filebrowser': dict(port=8080, path='/drive/', cmd=[f'{p}/bin/filebrowser', '-d', f'{h}/.config/filebrowser/filebrowser.db', '-a', '127.0.0.1', '-c', f'{h}/.config/filebrowser/fb_config.json', '--cacheDir', f'{h}/.cache/filebrowser', '--disableImageResolutionCalc', '--imageProcessors', '2']),
        'transfer_gateway': dict(port=8081, path='/healthz', cmd=[f'{h}/bin/mobile-gateway']),
        'adguard': dict(port=3000, path='/', cmd=[f'{h}/adguard/AdGuardHome/AdGuardHome', '-c', f'{h}/adguard/data/AdGuardHome.yaml', '-w', f'{h}/adguard/data', '--no-permcheck', '--no-etc-hosts']),
        'syncthing': dict(port=8384, path='/rest/noauth/health', cmd=[f'{p}/bin/syncthing', 'serve', '--config', f'{h}/.local/state/syncthing', '--data', f'{h}/.local/state/syncthing', '--allow-newer-config', '--no-browser']),
        'vaultwarden': dict(port=8082, path='/alive', cmd=[f'{p}/bin/proot-distro', 'login', 'debian', '--', '/usr/bin/env', 'ROCKET_PORT=8082', 'ROCKET_ADDRESS=127.0.0.1', f'DATA_FOLDER={h}/vaultwarden/data', f'WEB_VAULT_FOLDER={h}/vaultwarden/web-vault', f'{h}/vaultwarden/vaultwarden']),
        'terminal': dict(port=7681, path='/terminal/', cmd=[f'{p}/bin/ttyd', '-W', '-i', '127.0.0.1', '-p', '7681', '-b', '/terminal', '-I', f'{h}/.config/ttyd/termux_index.html', f'{h}/bin/termux_session.sh']),
        'agy': dict(port=7682, path='/agy/', cmd=[f'{p}/bin/ttyd', '-W', '-i', '127.0.0.1', '-p', '7682', '-b', '/agy', '-I', f'{h}/.config/ttyd/agy_index.html', f'{h}/bin/agy_session.sh']),
        'laptop_terminal': dict(port=7683, path='/laptop/', cmd=[f'{p}/bin/ttyd', '-W', '-a', '-O', '-m', '8', '-i', '127.0.0.1', '-p', '7683', '-b', '/laptop', f'{p}/bin/python3', f'{h}/bin/laptop_session.py']),
        'tor': dict(port=9050, protocol='socks5', cmd=[f'{p}/bin/tor', '-f', f'{h}/.config/tor/torrc']),
        'battery_guard': dict(cmd=[f'{p}/bin/bash', f'{h}/bin/battery_guard.sh', '--daemon']),
    }


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
        subprocess.run([str(PREFIX / 'bin/tmux'), 'kill-session', '-t', '=agy'], timeout=4, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


def launch(sid, cfg):
    log_path = STATE / f'{sid}.log'
    # Rotate before restart; do not truncate a running writer's file.
    if log_path.exists() and log_path.stat().st_size > 2*1024*1024:
        log_path.replace(log_path.with_suffix('.log.1'))
    env = os.environ.copy()
    env.update(HOME=str(HOME), PREFIX=str(PREFIX), PATH=f'{PREFIX}/bin:{PREFIX}/bin/applets:' + env.get('PATH', ''), GOMEMLIMIT='256MiB', GOGC='100')
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
                    self.children.append(launch(sid, cfg))
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
