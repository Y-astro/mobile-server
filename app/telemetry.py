"""Sample actual host telemetry once, independently of HTTP request volume."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import threading
import time


def bounded_command(args, timeout=3):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=timeout)
        if process.returncode:
            raise OSError('Telemetry command failed')
        return stdout
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise


class Telemetry:
    def __init__(self, home=None, interval=30):
        self.home = Path(home or Path.home())
        self.interval = interval
        self._lock = threading.Lock()
        self._started = False
        self._snapshot = self.empty()
        self._previous_cpu = None
        self._previous_net = None

    @staticmethod
    def empty():
        return dict(
            tailscale_ip=os.environ.get('MOBILE_SERVER_TAILSCALE_IP', '100.75.159.53'),
            local_ip=None, uptime=None, cpu_temp=None, cpu_percent=None,
            load_avg=[None, None, None],
            battery=dict(percentage=None, temperature=None, status=None, health=None, plugged=None, alert_state=None, last_updated=None),
            ram=dict(used_mb=None, total_mb=None, percent=None),
            storage=dict(used_gb=None, total_gb=None, free_gb=None, percent=None),
            network=dict(wifi_speed=None, rssi=None, frequency=None, wan_ping=None, lan_ping=None, dns_status=None, adguard_online=None, rx_mib_s=None, tx_mib_s=None),
            sampled_at=None, sources={},
        )

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._loop, name='host-telemetry', daemon=True).start()

    def snapshot(self):
        self.start()
        with self._lock:
            result = copy.deepcopy(self._snapshot)
        stamp = result.get('sampled_at')
        result['stale'] = stamp is None or time.time()-stamp > self.interval*3
        return result

    def _loop(self):
        while True:
            try:
                sample = self.collect()
                with self._lock:
                    self._snapshot = sample
            except Exception:
                # Preserve last sample and its original timestamp on collector error.
                pass
            time.sleep(self.interval)

    def collect(self):
        result = self.empty()
        now = time.time()
        sources = result['sources']
        # Android may prohibit /proc readers. Missing values stay null.
        try:
            mem = {}
            for line in Path('/proc/meminfo').read_text().splitlines():
                key, value = line.split(':', 1)
                mem[key] = int(value.split()[0])
            total = mem['MemTotal']/1024
            available = mem.get('MemAvailable', mem['MemFree'])/1024
            result['ram'] = dict(total_mb=round(total, 1), used_mb=round(total-available, 1), percent=round(100*(total-available)/total, 1))
            sources['ram'] = '/proc/meminfo'
        except (OSError, ValueError, KeyError):
            pass
        try:
            seconds = int(time.clock_gettime(time.CLOCK_BOOTTIME))
            result['uptime'] = f'{seconds//86400}d {(seconds%86400)//3600}h {(seconds%3600)//60}m'
            sources['uptime'] = 'CLOCK_BOOTTIME'
        except (OSError, AttributeError):
            pass
        try:
            result['load_avg'] = [round(v, 2) for v in os.getloadavg()]
            sources['load_avg'] = 'getloadavg'
        except OSError:
            pass
        try:
            fields = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(fields), fields[3]+fields[4]
            if self._previous_cpu:
                dt, di = total-self._previous_cpu[0], idle-self._previous_cpu[1]
                result['cpu_percent'] = round(100*(dt-di)/dt, 1) if dt > 0 else None
            self._previous_cpu = (total, idle)
            sources['cpu_percent'] = '/proc/stat'
        except (OSError, ValueError, IndexError):
            pass
        for root in (Path('/storage/emulated/0'), self.home):
            try:
                usage = shutil.disk_usage(root)
                result['storage'] = dict(used_gb=round(usage.used/1024**3, 1), total_gb=round(usage.total/1024**3, 1), free_gb=round(usage.free/1024**3, 1), percent=round(usage.used/usage.total*100, 1))
                sources['storage'] = str(root)
                break
            except OSError:
                pass
        battery_file = self.home/'.config/battery_guard/status.json'
        try:
            if now-battery_file.stat().st_mtime < 300:
                data = json.loads(battery_file.read_text())
                result['battery'].update(data)
                result['battery']['last_updated'] = battery_file.stat().st_mtime
                sources['battery'] = 'battery_guard/status.json'
        except (OSError, ValueError):
            pass
        if result['battery']['percentage'] is None:
            try:
                data = json.loads(bounded_command(['termux-battery-status']))
                for key in ('percentage', 'temperature', 'status', 'health', 'plugged'):
                    result['battery'][key] = data.get(key)
                result['battery']['last_updated'] = now
                sources['battery'] = 'termux-battery-status'
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        # Battery temperature is measured independently; never label it CPU temperature.
        for zone in sorted(Path('/sys/class/thermal').glob('thermal_zone*')):
            try:
                kind = (zone/'type').read_text().strip().lower()
                if 'cpu' in kind:
                    value = float((zone/'temp').read_text())
                    result['cpu_temp'] = round(value/1000 if value > 1000 else value, 1)
                    sources['cpu_temp'] = str(zone)
                    break
            except (OSError, ValueError):
                pass
        try:
            wifi = json.loads(bounded_command(['termux-wifi-connectioninfo']))
            result['network'].update(wifi_speed=wifi.get('link_speed_mbps'), rssi=wifi.get('rssi'), frequency=wifi.get('frequency_mhz'))
            result['local_ip'] = wifi.get('ip')
            sources['wifi'] = 'termux-wifi-connectioninfo'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if result['local_ip'] is None:
            try:
                interfaces = json.loads(bounded_command(['ip', '-j', '-4', 'addr', 'show', 'wlan0']))
                result['local_ip'] = next(a['local'] for i in interfaces for a in i.get('addr_info', []) if a.get('scope') == 'global')
                sources['local_ip'] = 'ip address'
            except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired):
                pass
        try:
            text = bounded_command(['ping', '-n', '-c', '1', '-W', '1', '1.1.1.1'], timeout=2)
            match = re.search(r'time[=<]([\d.]+)', text)
            result['network']['wan_ping'] = float(match[1]) if match else None
            sources['wan_ping'] = 'ICMP 1.1.1.1'
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            counters = tuple(int(Path(f'/sys/class/net/wlan0/statistics/{k}_bytes').read_text()) for k in ('rx', 'tx'))
            if self._previous_net:
                elapsed = time.monotonic()-self._previous_net[0]
                result['network'].update({k:round(max(0,value-old)/1048576/elapsed, 3) for k,value,old in zip(('rx_mib_s','tx_mib_s'), counters, self._previous_net[1])})
            self._previous_net = (time.monotonic(), counters)
            sources['network_rates'] = 'wlan0 byte counters'
        except (OSError, ValueError):
            pass
        try:
            with socket.create_connection(('127.0.0.1', 3000), timeout=.2):
                result['network']['adguard_online'] = True
        except OSError:
            result['network']['adguard_online'] = False
        result['network']['dns_status'] = 'AdGuard interface reachable' if result['network']['adguard_online'] else 'AdGuard interface unreachable'
        result['sampled_at'] = now
        return result
