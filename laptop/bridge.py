#!/usr/bin/python3
"""Forced SSH command: manage only the website's isolated tmux sessions."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import sys
import uuid

SOCKET = 'mobile-server-laptop'
PREFIX = 'laptop-'
ID = re.compile(r'[0-9a-f]{32}\Z')
HOME = Path.home()
STATE = HOME/'.local/state/mobile-server-laptop'
MAX_SESSIONS = 4


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=6, **kwargs)


def tmux(*args, check=True):
    result = run(['/usr/bin/tmux', '-L', SOCKET, '-f', '/dev/null', *args])
    if check and result.returncode:
        raise RuntimeError('Terminal session operation failed ('+args[0]+'): '+result.stderr.strip()[:200])
    return result


def validate_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('Invalid terminal ID')
    return value


def sessions():
    result = tmux('list-sessions', '-F', '#{session_name}\t#{session_created}\t#{session_attached}', check=False)
    if result.returncode:
        return []
    found = []
    for line in result.stdout.splitlines():
        name, created, attached = line.split('\t')
        if name.startswith(PREFIX) and ID.fullmatch(name[len(PREFIX):]):
            found.append(dict(id=name[len(PREFIX):], created_at=int(created), attached_clients=int(attached)))
    return sorted(found, key=lambda row: row['created_at'])


def desktop_environment():
    env = os.environ.copy()
    env['XDG_RUNTIME_DIR'] = f'/run/user/{os.getuid()}'
    env['DBUS_SESSION_BUS_ADDRESS'] = f"unix:path={env['XDG_RUNTIME_DIR']}/bus"
    result = run(['/usr/bin/systemctl', '--user', 'show-environment'], env=env)
    desktop = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'PATH', 'SSH_AUTH_SOCK', 'XDG_CURRENT_DESKTOP'):
        if key in desktop:
            env[key] = desktop[key]
    wayland = env.get('WAYLAND_DISPLAY')
    if not (wayland and (Path(env['XDG_RUNTIME_DIR'])/wayland).exists()) and not env.get('DISPLAY'):
        raise RuntimeError('Laptop desktop is not logged in; Kitty cannot open')
    return env


def launch_kitty(session_id):
    env = desktop_environment()
    unit = 'mobile-server-kitty-' + session_id
    command = ['/usr/bin/systemd-run', '--user', '--collect', '--quiet', '--unit='+unit, '--property=Type=exec']
    for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
        if env.get(key):
            command += ['--setenv='+key+'='+env[key]]
    command += ['--', '/usr/bin/kitty', '--title=Server laptop terminal '+session_id[:8], '--directory='+str(HOME),
                '/usr/bin/tmux', '-L', SOCKET, 'attach-session', '-t', PREFIX+session_id]
    result = run(command, env=env)
    if result.returncode:
        raise RuntimeError('Could not open Kitty in the laptop desktop')


def dispatch(args):
    if not args or len(args) > 2:
        raise ValueError('Unsupported operation')
    action = args[0]
    if action == 'list' and len(args) == 1:
        return dict(online=True, sessions=sessions(), max_sessions=MAX_SESSIONS)
    if action == 'create' and len(args) == 1:
        if len(sessions()) >= MAX_SESSIONS:
            raise RuntimeError('Close a terminal before opening another (maximum four)')
        desktop = desktop_environment()  # Fail before starting a shell when no desktop exists.
        ident = uuid.uuid4().hex
        name = PREFIX+ident
        tmux('new-session', '-d', '-s', name, '-n', 'laptop', '-x', '100', '-y', '30', '-c', str(HOME), 'sleep 60')
        try:
            tmux('set-window-option', '-t', name+':', 'history-limit', '50000')
            for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'PATH', 'SSH_AUTH_SOCK', 'XDG_CURRENT_DESKTOP'):
                if desktop.get(key):
                    tmux('set-environment', '-t', name, key, desktop[key])
            tmux('set-option', '-t', name, 'mouse', 'on')
            tmux('set-option', '-t', name, 'status', 'off')
            tmux('set-window-option', '-t', name+':', 'window-size', 'latest')
            tmux('respawn-window', '-k', '-t', name+':', pwd.getpwuid(os.getuid()).pw_shell)
            launch_kitty(ident)
        except Exception:
            tmux('kill-session', '-t', name, check=False)
            raise
        return dict(id=ident, created=True)
    if action in ('attach', 'close') and len(args) == 2:
        ident = validate_id(args[1])
        name = PREFIX+ident
        exists = any(row['id'] == ident for row in sessions())
        if action == 'close':
            if exists:
                tmux('kill-session', '-t', name)
            # Kitty exits with its tmux client; closing never targets unrelated windows.
            return dict(id=ident, closed=True)
        if not exists:
            raise ValueError('Terminal session no longer exists')
        return ('attach', name)
    raise ValueError('Unsupported operation')


def main():
    # Never evaluate SSH_ORIGINAL_COMMAND as a shell command.
    args = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE/'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = dispatch(args)
    if isinstance(result, tuple):
        os.execv('/usr/bin/tmux', ['tmux', '-L', SOCKET, 'attach-session', '-t', result[1]])
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'error':str(error)}))
        raise SystemExit(1)
