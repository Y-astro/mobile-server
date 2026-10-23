#!/usr/bin/env python3
"""Install the laptop's user service with the phone's dedicated public key."""
import argparse
import ipaddress
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import re

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--phone-public-key',required=True,type=Path)
p.add_argument('--laptop-ip',default='100.111.177.30',type=ipaddress.IPv4Address)
p.add_argument('--phone-ip',default='100.75.159.53',type=ipaddress.IPv4Address)
a=p.parse_args()
key=a.phone_public_key.read_text().strip()
if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?',key):p.error('Expected a single Ed25519 public key')
home=Path.home();root=home/'.config/mobile-server-laptop';root.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(root,0o700)
shutil.copyfile(Path(__file__).with_name('bridge.py'),root/'bridge.py');os.chmod(root/'bridge.py',0o700)
private=root/'host_ed25519'
if not private.exists():subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(private)],check=True)
(root/'authorized_keys').write_text(f'from="{a.phone_ip}",restrict,pty,command="{root}/bridge.py" {key}\n');os.chmod(root/'authorized_keys',0o600)
config=f'''Port 2222
ListenAddress {a.laptop_ip}
HostKey {private}
PidFile {root}/sshd.pid
AuthorizedKeysFile {root}/authorized_keys
AllowUsers {pwd.getpwuid(os.getuid()).pw_name}
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
PermitTTY yes
AllowTcpForwarding no
AllowStreamLocalForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitUserRC no
PermitUserEnvironment no
LoginGraceTime 20
MaxAuthTries 2
MaxSessions 8
MaxStartups 4:50:8
ClientAliveInterval 15
ClientAliveCountMax 3
UseDNS no
'''
(root/'sshd_config').write_text(config);os.chmod(root/'sshd_config',0o600)
subprocess.run(['/usr/bin/sshd','-t','-f',str(root/'sshd_config')],check=True)
units=home/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
(units/'mobile-server-laptop.service').write_text(f'''[Unit]
Description=Phone server laptop terminal bridge (Tailscale only)
StartLimitIntervalSec=0

[Service]
ExecStart=/usr/bin/sshd -D -e -f {root}/sshd_config
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
''')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now','mobile-server-laptop.service'],check=True)
print('Laptop bridge installed. Copy host_ed25519.pub to the phone known_hosts with [laptop-IP]:2222 prefix; never copy host private key.')
