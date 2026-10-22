"""Control a real Tor SOCKS proxy and report verified proxy egress only."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from telemetry import bounded_command


class TorProxy:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self._lock = threading.Lock()
        self._started = False
        self._wake = threading.Event()
        self._status = dict(enabled=False, configured=True, mode='socks5', full_device_routing=False,
                            verified_via_tor=False, direct_ip=None, masked_ip=None,
                            masked_isp=None, masked_location=None, direct_isp=None,
                            direct_location=None, checked_at=None, error=None,
                            socks_endpoint='127.0.0.1:9050')

    def desired(self):
        return not (self.home/'.config/services_disabled/tor').exists()

    def snapshot(self):
        with self._lock:
            if not self._started:
                self._started = True
                threading.Thread(target=self._loop, name='tor-egress-check', daemon=True).start()
            state = dict(self._status)
        state['enabled'] = self.desired()
        state['stale'] = state['checked_at'] is None or time.time()-state['checked_at'] > 120
        if not state['enabled'] or state['stale']:
            state['verified_via_tor'] = False
            state['masked_ip'] = None
        return state

    def set_enabled(self, enabled):
        result = subprocess.run(['python3', str(self.home/'bin/server_supervisor.py'), '--service', 'tor', '--enable', str(enabled).lower()], capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise RuntimeError('Tor control failed; check supervisor logs')
        with self._lock:
            self._status['verified_via_tor'] = False
            self._status['masked_ip'] = None
        self._wake.set()
        return self.snapshot()

    def _lookup(self, through_tor):
        args = ['curl', '--silent', '--show-error', '--fail', '--connect-timeout', '5', '--max-time', '18']
        if through_tor:
            args += ['--proxy', 'socks5h://127.0.0.1:9050', '--noproxy', '']
        else:
            args += ['--noproxy', '*']
        args += ['https://check.torproject.org/api/ip']
        data = json.loads(bounded_command(args, timeout=20))
        address = str(ipaddress.ip_address(data['IP']))
        if through_tor and data.get('IsTor') is not True:
            raise ValueError('Proxy response is not a Tor exit')
        return address

    def _loop(self):
        while True:
            state = dict(error=None, checked_at=time.time(), direct_ip=None, masked_ip=None, verified_via_tor=False)
            try:
                state['direct_ip'] = self._lookup(False)
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                pass
            enabled = self.desired()
            if enabled:
                try:
                    state['masked_ip'] = self._lookup(True)
                    state['verified_via_tor'] = True
                except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                    state['error'] = 'Tor is starting or its exit check failed; proxy egress is not verified yet.'
            # Disabling while the check was in flight must discard its result.
            if not self.desired():
                state['masked_ip'], state['verified_via_tor'] = None, False
            with self._lock:
                self._status.update(state)
            self._wake.wait(30)
            self._wake.clear()
