# Laptop Kitty terminals

The admin dashboard's **Laptop terminals** card opens Kitty on the laptop and provides a browser terminal controlling the same shell. Files, commands and processes in that terminal belong to the laptop user, not the Redmi.

1. Keep the laptop awake, connected to Tailscale, and logged in to its desktop.
2. Click **New terminal** on the server dashboard. A Kitty window opens on the laptop and a browser tab opens the same shell. If the browser blocks the new tab, click **Reconnect** on the card.
3. Close the browser tab to disconnect while commands keep running. Use **Reconnect** to return.
4. Click **Close** to end that terminal and its running commands. Other Kitty windows and tmux sessions are unaffected.

Up to four managed terminals can run at once. A computer restart ends these tmux shells; they are not disk-persisted jobs. Sleeping/disconnected laptops show an offline state, and Kitty creation requires a logged-in desktop. This shares a terminal shell, not a graphical desktop or Kitty's GPU renderer. Existing arbitrary Kitty windows are not exposed automatically.

## Connection and access

```
Browser → admin-authenticated Redmi website → phone ttyd :7683
                                         → SSH over Tailscale → laptop user SSH service :2222
                                                               → managed tmux session
                                                               ← local Kitty attaches here
```

The browser stays on the server website; it does not connect directly to a companion HTTP service on the laptop. This avoids cross-origin/local-network permission dependencies. Phone ttyd is loopback-only and its WebSocket origin is checked. The `/laptop*` route and terminal API require the dashboard's admin role. Family accounts cannot access them, including encoded path variants.

The laptop's dedicated unprivileged SSH service listens only on its Tailscale address. Password login, root login, forwarding, agent forwarding and X11 forwarding are disabled. The phone uses a separate Ed25519 key and pins the laptop's host key. The authorized key permits only the phone's Tailscale IP and runs the fixed bridge program. The bridge accepts `list`, `create`, `attach <id>`, and `close <id>`; IDs must be 32 lowercase hexadecimal characters and are never evaluated as commands. Terminal URL arguments are validated independently on the phone.

This is full shell access as the laptop user, so dashboard administrators can access that user's files and run programs with their usual permissions. The key restrictions isolate this integration's control protocol; they are not a sandbox once an authorized terminal is attached. No sudo or system-wide SSH service changes are needed.

## Installed components

Laptop:

- `~/.config/mobile-server-laptop/bridge.py`
- Private host key, dedicated `authorized_keys`, and `sshd_config` in that same private directory.
- `~/.config/systemd/user/mobile-server-laptop.service`, enabled for the user session.
- A dedicated tmux server named `mobile-server-laptop` with sessions `laptop-<id>`.
- Per-window transient units `mobile-server-kitty-<id>.service`.

Phone:

- `~/.config/mobile-server-laptop/client.json`, identity key and pinned `known_hosts`, all private runtime files.
- `~/.local/lib/mobile-server/laptop.py`, `~/bin/laptop_session.py` and supervisor service `laptop_terminal`.
- Loopback ttyd on port 7683, proxied by the website at `/laptop/`.

The desktop environment is obtained from the laptop's systemd user manager so SSH-launched Kitty uses the active Wayland/X11 desktop. Supported desktop variables and the local agent socket are passed to the managed shell; SSH does not forward another machine's agent.

## Provisioning another installation

This checkout's normal `scripts/install.sh` installs the phone code but deliberately does not generate or publish pairing credentials. Pair devices over an already trusted administrative connection:

1. On the phone, create a dedicated Ed25519 key at `~/.config/mobile-server-laptop/id_ed25519` with mode 600; export only its `.pub` file.
2. On the laptop, run `python3 laptop/install.py --phone-public-key /path/to/phone.pub --laptop-ip <laptop-Tailscale-IP> --phone-ip <phone-Tailscale-IP>`. This installs the bridge, generates a private host key if none exists, validates its dedicated SSH config, and enables the user service. The existing system SSH service/configuration and ordinary `~/.ssh/authorized_keys` are not modified.
3. Transfer the laptop's **public** host key back over the trusted channel. Pin it in the phone's dedicated `known_hosts` as `[laptop-Tailscale-IP]:2222 ssh-ed25519 PUBLIC-KEY`. Do not use unverified `ssh-keyscan` output as trust establishment and do not copy the laptop's private host key.
4. Write the phone's private `client.json` with `host`, `user`, `identity_file`, and `known_hosts_file` pointing at those phone paths. The current implementation uses port 2222.
5. Ensure the tailnet ACL permits phone-to-laptop TCP 2222, then check the card. No public router port forwarding or USB connection is needed.

The installer uses the current user's home and existing Kitty, tmux, Python, OpenSSH and systemd user manager. It targets this Linux laptop; it does not provision Windows/macOS services. Display environments should be imported by the desktop session as they are on the configured Hyprland laptop.

## Troubleshooting and removal

On the laptop:

```sh
systemctl --user status mobile-server-laptop.service
journalctl --user -u mobile-server-laptop.service -n 30
systemctl --user list-units 'mobile-server-kitty-*'
tmux -L mobile-server-laptop list-sessions
```

On the phone, `server-health` reports the `laptop_terminal` gateway process; that being healthy does not mean the laptop itself is online. The dashboard reports laptop connectivity separately. If SSH host-key checking fails after a reinstall, verify the new public host key through a trusted channel before replacing the pin.

To disable new access, run `systemctl --user disable --now mobile-server-laptop.service` on the laptop and disable `laptop_terminal` with the phone supervisor. Stopping the SSH service may disconnect clients; managed tmux shells/Kitty windows can remain. Close each managed terminal explicitly if you also want its commands stopped. Keep unrelated terminal sessions untouched.

References: [Kitty invocation](https://sw.kovidgoyal.net/kitty/invocation/), [OpenSSH key restrictions](https://man.openbsd.org/sshd), [ttyd options](https://github.com/tsl0922/ttyd).
