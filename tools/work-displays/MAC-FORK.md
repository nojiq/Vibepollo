# Personal Mac work displays

Branch `feature/macos-work-displays` adds automatic arrangement memory to the
upstream macOS Remote Monitor implementation. Pairings and configuration stay
in `~/.config/vibepollo`.

Arrange screens in macOS System Settings → Displays. After the arrangement is
stable for half a second, Vibepollo saves each connected Remote Monitor's
position relative to the main physical screen. Absent clients keep their last
position; reconnect order does not change it. Physical screens use stable UUID
anchors. A missing physical anchor temporarily places the monitor on the right
without replacing its saved position. The web interface's fixed layout editor
can replace these placements; use macOS Displays for automatic memory.

Disconnected monitors remain available for Resume for 30 seconds, then their
virtual display is removed. Each client's grace period is independent. An
accepted launch that never establishes a stream is also cleaned up. Reconnect
creates the display and restores the saved placement. Normal upstream limits
remain: the Mac must stay awake with its lid open, and virtual displays depend
on private macOS interfaces. No virtual gamepads are added.

`ci-macos.yml` can build Apple Silicon only using workflow input
`target_arch=arm64`. Without a Developer ID certificate, the bundle is ad-hoc
signed and not notarized. macOS may require Open Anyway and screen recording /
accessibility grants for the new binary. Existing pairings are preserved.

The Surface's company-network reconnect service is described in README.md.
MSI Moonlight reconnect automation must be installed separately on Windows;
this packet changes only the authorized Surface client.
