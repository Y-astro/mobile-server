"""Bounded phone-side client for the laptop's restricted Tailscale SSH bridge."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

ID = re.compile(r'[0-9a-f]{32}\Z')


class LaptopError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def validate_id(value):
    if not isinstance(value,str) or not ID.fullmatch(value):
        raise LaptopError('Invalid terminal ID',400)
    return value


class LaptopClient:
    def __init__(self, config=None):
        self.path = Path(config or Path.home()/'.config/mobile-server-laptop/client.json')
        self._lock = threading.Lock()
        self._checked = 0
        self._status = None
        self._operation = threading.Lock()

    def ssh_args(self, command, terminal=False):
        try:
            config = json.loads(self.path.read_text())
            host,user = config['host'],config['user']
            if not re.fullmatch(r'[0-9.]+',host) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user):raise ValueError()
            key=str(Path(config['identity_file']).expanduser());known=str(Path(config['known_hosts_file']).expanduser())
            if not Path(key).is_file() or not Path(known).is_file():raise ValueError()
        except (OSError,ValueError,KeyError,TypeError):
            raise LaptopError('Laptop connection has not been paired',503)
        return ['ssh','-tt' if terminal else '-T','-F','/dev/null','-p','2222','-i',key,
                '-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
                '-o','UserKnownHostsFile='+known,'-o','ConnectTimeout=3','-o','ConnectionAttempts=1',
                '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',user+'@'+host,command]

    def _call(self, command):
        try:
            result = subprocess.run(self.ssh_args(command),capture_output=True,text=True,timeout=12)
        except subprocess.TimeoutExpired:
            raise LaptopError('Laptop did not respond; check that it is awake and connected to Tailscale')
        except OSError:
            raise LaptopError('Could not start the laptop connection')
        if result.returncode == 255:
            raise LaptopError('Laptop offline or SSH pairing unavailable')
        try:
            data=json.loads(result.stdout)
        except (ValueError,TypeError):
            raise LaptopError('Laptop returned an invalid response')
        if not isinstance(data,dict):raise LaptopError('Laptop returned an invalid response')
        if result.returncode or data.get('error'):
            raise LaptopError(data.get('error','Laptop operation failed'),409)
        return data

    def status(self):
        with self._lock:
            if self._status is None or time.monotonic()-self._checked>5:
                try:
                    data=self._call('list')
                    self._status=dict(online=True,sessions=data['sessions'],max_sessions=data['max_sessions'])
                except LaptopError as error:
                    self._status=dict(online=False,sessions=[],error=str(error))
                self._checked=time.monotonic()
            return dict(self._status)

    def operation(self, action, ident=None):
        if action not in ('create','close'):raise LaptopError('Unsupported terminal operation',400)
        if action=='close':validate_id(ident)
        if not self._operation.acquire(blocking=False):raise LaptopError('Another terminal operation is in progress',429)
        try:
            result=self._call(action if ident is None else action+' '+ident)
            with self._lock:self._checked=0;self._status=None
            return result
        finally:self._operation.release()
