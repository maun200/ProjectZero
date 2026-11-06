# Add a worker device over an SSH tunnel

A device (e.g. a Fedora laptop) reaches the coordinator only through SSH with its own key. On the
coordinator host that key may open exactly one forward, to `127.0.0.1:8765`: no shell, no files, no other
ports. The coordinator itself stays on loopback; nothing is opened on the network besides the SSH server.

| Script | Runs on | Purpose |
|---|---|---|
| `scripts/grid_host_setup.sh` | coordinator host, once, `sudo` | user `gridtunnel` (no shell/home), root-owned key file, sshd drop-in limited to this user |
| `scripts/grid_device.py` | coordinator host | `add` / `remove` / `list` devices: worker token + restricted key line + device bundle |
| `scripts/grid_join.py` | device | `init` key, `check` tunnel, `run` tunnel + worker with reconnect |

## 1. Coordinator host (once)

```bash
sudo scripts/grid_host_setup.sh            # prints the effective settings for gridtunnel
# rollback: sudo scripts/grid_host_setup.sh --rollback
```

The global sshd configuration and other users are unchanged. Your own account may still allow password
logins over SSH (OpenSSH default); consider `PasswordAuthentication no` once you have a key login of your own.

Start the coordinator on loopback (keep this terminal open, or use tmux):

```bash
scripts/run_coordinator.sh    # first run creates ~/.config/community-grid/coordinator.env (0600, random keys)
```

Approved models are set in that file (`CAG_ALLOWED_MODELS`, default `qwen2.5-coder:1.5b`).

## 2. Device (Fedora example)

```bash
sudo dnf install -y git python3 openssh-clients
git clone <your private repo URL> community-ai-grid && cd community-ai-grid
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock && .venv/bin/pip install --no-deps -e .
```

Install a local model server and **only an operator-approved model**. For 8 GB RAM and a 10th-gen i5 without
dedicated GPU, `qwen2.5-coder:1.5b` (Q4_K_M, ~1 GB, Apache-2.0) fits; Ollama listens on loopback by default.
Downloading weights is the operator's decision:

```bash
curl -fsSL https://ollama.com/install.sh | sh    # review the script first if you prefer
ollama pull qwen2.5-coder:1.5b
.venv/bin/python scripts/grid_join.py init       # prints the device's PUBLIC key
```

Copy the printed public key line to the coordinator host (it is not secret).

## 3. Coordinator host: add the device

```bash
# reads the admin key from coordinator.env; asks for your sudo password to update the root-owned key file
.venv/bin/python scripts/grid_device.py add fedora-laptop --pubkey fedora-laptop.pub \
    --host <THIS COORDINATOR'S OWN address, as reachable from the device — its Tailscale name/IP or LAN IP; NEVER the device's address> \
    --model qwen2.5-coder:1.5b
```

**2026-11-04, clarified after a real mix-up between two coordinating sessions:** `--host` is
written verbatim into the device's bundle as `CAG_TUNNEL_HOST` — it is purely where the *device*
points its outbound SSH tunnel, i.e. this coordinator host's own address, never the device's own
address. Registering a device needs exactly two things from the device side: its public key
(from `grid_join.py init`, not secret) and a name — nothing about the device's own reachability.
If the device is not on the same LAN as this coordinator host, giving the device a different
`--host` does not help; the **coordinator host itself** needs its own Tailscale (or equivalent
overlay) identity, since the device dials out to it, not the other way around.

This writes `~/.config/community-grid/devices/fedora-laptop.env` (mode 0600). It contains the **worker
token** and the pinned SSH host key. Move it privately (USB stick, or `scp` run on the laptop with your
own account on the host) to the laptop as `~/.config/community-grid/worker.env`, then delete it on the host.

## 4. Device: connect

```bash
chmod 600 ~/.config/community-grid/worker.env
.venv/bin/python scripts/grid_join.py check      # "Tunnel up: coordinator /health OK"
.venv/bin/python scripts/grid_join.py run        # Ctrl+C stops tunnel and worker
```

`run` restarts a dropped tunnel with backoff (2–60 s); the worker keeps polling meanwhile. Device-local
settings (`CAG_OLLAMA_NUM_THREAD`, `CAG_OLLAMA_NUM_CTX`, `CAG_OLLAMA_NUM_GPU`, ...) may be edited in
`worker.env`. The host key is pinned from the bundle; a changed host key stops the tunnel.

Optional autostart as a systemd **user** service:

```ini
# ~/.config/systemd/user/community-grid.service
[Unit]
Description=Community AI Grid worker
After=network-online.target

[Service]
WorkingDirectory=%h/community-ai-grid
ExecStart=%h/community-ai-grid/.venv/bin/python scripts/grid_join.py run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

`systemctl --user daemon-reload && systemctl --user enable --now community-grid` (add
`loginctl enable-linger $USER` to run without being logged in).

## Remove a device

```bash
.venv/bin/python scripts/grid_device.py remove fedora-laptop
```

Removes the key line (new tunnels fail) and revokes the worker token (an already open tunnel can no
longer claim or report jobs).

## Tested / not tested

- Tested: `tests/test_grid_device.py` (validation, rollback, bundle) and `tests/test_grid_tunnel.py` against a
  real, unprivileged OpenSSH server on loopback: tunnel works, other ports and commands are refused, host-key
  mismatch is refused, and these tests fail when the key restrictions are removed. One end-to-end run on one
  host: add → check → job with `qwen2.5-coder:1.5b` on CPU completed through the tunnel → remove → tunnel refused.
- Tested 2026-11-03 with a real second device (Fedora laptop, i5 10th gen, 8 GB RAM, CPU only):
  `grid_host_setup.sh` on the host's system sshd (run by the operator), `add` → bundle via `scp` → `check` →
  `run`; two real jobs completed through the tunnel; SIGTERM to the tunnel's ssh process during `run`
  (orderly shutdown, ssh exit 0) led to "reconnecting in 2 s" and a new tunnel while the worker kept running;
  SIGKILL (ssh exit -9) likewise reconnected after 2 s; a ~51 s Wi-Fi outage (`nmcli radio wifi off`)
  did **not** drop the tunnel: the same ssh process carried on and `/health` worked again right after Wi-Fi
  returned, with one failed worker request (30 s client timeout) in between. OpenSSH only gives up after
  more than `ServerAliveCountMax` (3) unanswered probes at 15 s intervals, i.e. roughly 45–60 s without any
  reply, and TCP plus Tailscale kept the session alive meanwhile. A job after these tests completed;
  the systemd user unit runs
  (without `enable-linger`, so it starts after login).
- Not tested: outages longer than ~60 s (expected: ssh exits, then reconnect with backoff), switching networks,
  suspend/resume on the device, Windows devices, several devices at once.
