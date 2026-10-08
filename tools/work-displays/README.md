# Surface Moonlight reconnect helper

This user service keeps one paired Moonlight `Remote Monitor` stream connected
while the Surface is on the trusted company network. It is intended for the
Zorin Surface client and the Vibepollo Mac host.

The helper:

- accepts company Ethernet only when its address is in the configured company
  CIDR; Wi-Fi additionally requires a trusted SSID;
- resolves the paired host's Moonlight manual address first, then uses the
  saved local address as a fallback, so a DHCP change is handled;
- reads only the selected host's saved server certificate from the Flatpak
  profile (falling back to the native Moonlight profile) and verifies the peer
  certificate on TLS 47984 or 47990 before asking Moonlight for its app list;
- chooses `Remote Monitor`, falling back to `Resume` for the current fork's
  app list;
- retries with bounded exponential backoff (5, 10, 20, 40, 80, then 120
  seconds by default);
- stops the stream it started after 30 seconds outside the company network;
- uses an advisory lock and `/proc` scan to avoid starting a second stream;
- starts Moonlight in its own process group, so disconnect cleanup cannot kill
  unrelated desktop processes;
- watches at least two owned, company-bound descendant UDP sockets during
  startup and restarts it if the v6.2.0 CLI remains open behind a failed-stream
  error dialog; DNS, mDNS, SSDP, wildcard, loopback, and off-subnet rows do not
  count;
- verifies the pinned Sunshine certificate while connected and stops/retries
  after 5 seconds of host loss; after startup, a known zero-socket session is
  restarted after the same 5-second grace, while an unavailable `ss` probe is
  treated as unknown and never kills a healthy stream;
- retries every exit from a stream it started, including a clean exit during a
  host restart; a pre-existing Moonlight stream is left alone in manual hold,
  and the hold clears automatically after that stream disappears.

It never writes `Moonlight.conf`, changes pairing, or modifies Moonlight's
video settings. The stream flags are the existing Surface profile:
H.264, hardware decode, 2160x1440, 30 FPS, 12 Mbps, fullscreen.

Moonlight 6.2.0 transitions a failed CLI launch to its GUI error dialog; the
dialog closes the Qt application only when a user closes it. The 45-second
startup watchdog covers a lost host or app-search failure when the dialog is
left open. See the [v6.2.0 launcher source](https://github.com/moonlight-stream/moonlight-qt/blob/v6.2.0/app/cli/startstream.cpp)
and [CLI segue](https://github.com/moonlight-stream/moonlight-qt/blob/v6.2.0/app/gui/CliStartStreamSegue.qml).

## Install on the Surface

Copy `moonlight_reconnect.py`, `moonlight-remote-monitor`, and
`moonlight-remote-monitor.service` from this directory to the Surface. Then run
the following as `helpdesk`:

```bash
install -Dm755 moonlight_reconnect.py ~/.local/bin/moonlight_reconnect.py
install -Dm755 moonlight-remote-monitor ~/.local/bin/moonlight-remote-monitor
install -Dm644 moonlight-remote-monitor.service ~/.config/systemd/user/moonlight-remote-monitor.service
mkdir -p ~/.config/vibepollo
touch ~/.config/vibepollo/moonlight-reconnect.env
chmod 600 ~/.config/vibepollo/moonlight-reconnect.env
```

Put this policy in `~/.config/vibepollo/moonlight-reconnect.env`:

```ini
MOONLIGHT_HOST_PROFILE="Mac - Extended Displays"
MOONLIGHT_COMPANY_NETWORKS=192.168.8.0/24
MOONLIGHT_TRUSTED_SSIDS=JKS_2.4G,JKS_5G
MOONLIGHT_IDLE_DISCONNECT_SECONDS=30
# Kill/retry a live Moonlight GUI that never creates a streaming UDP session.
MOONLIGHT_STARTUP_TIMEOUT_SECONDS=45
# Host certificate health must fail continuously for this long after startup.
MOONLIGHT_HEALTH_ABSENCE_GRACE_SECONDS=5
```

Disable the old one-shot GUI autostart before enabling the service. Keep a
backup so rollback is easy:

```bash
mkdir -p ~/.config/autostart/disabled
mv ~/.config/autostart/moonlight-sunshine.desktop \
   ~/.config/autostart/disabled/moonlight-sunshine.desktop
systemctl --user daemon-reload
systemctl --user enable --now moonlight-remote-monitor.service
systemctl --user --no-pager status moonlight-remote-monitor.service
journalctl --user -u moonlight-remote-monitor.service -f
```

Do not add `JKS_Guest_2.4G` unless that network is intentionally trusted. The
helper accepts the currently used wired company connection by subnet.

Install the optional app-menu entry for restart and explicit stop actions:

```bash
install -Dm644 moonlight-work-display.desktop ~/.local/share/applications/moonlight-work-display.desktop
```

Open **Mac Work Display** from the app menu. The equivalent command is:

```bash
systemctl --user restart moonlight-remote-monitor.service
```

Closing a Moonlight stream started by this helper reconnects automatically after
the bounded retry delay. Choose **Stop Work Display** from the same app-menu
entry, or run the command below, when you want to opt out:

```bash
systemctl --user stop moonlight-remote-monitor.service
```

Starting or restarting the service resumes automatic reconnect.

To stop automatic reconnect without touching Moonlight pairing:

```bash
systemctl --user disable --now moonlight-remote-monitor.service
```

The helper needs an active graphical login for the Flatpak client. Do not use
`loginctl enable-linger`; it would start Moonlight outside the desktop session.

## Rollback

```bash
systemctl --user disable --now moonlight-remote-monitor.service
rm ~/.config/systemd/user/moonlight-remote-monitor.service
mv ~/.config/autostart/disabled/moonlight-sunshine.desktop \
   ~/.config/autostart/moonlight-sunshine.desktop
systemctl --user daemon-reload
```

The older Flatpak or AppImage entry can then be started normally. No pairing
state is removed by this helper.
