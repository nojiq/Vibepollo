#!/usr/bin/env python3
"""Keep one paired Moonlight Remote Monitor stream connected on company LAN.

The daemon is intentionally independent of Vibepollo.  It only invokes the
existing Flatpak client and never edits Moonlight's profile or pairing files.
"""

from __future__ import annotations

import dataclasses
import argparse
import fcntl
import hashlib
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import signal
import time
from typing import Iterable


MIN_STREAM_UDP_SOCKETS = 2
DISCOVERY_UDP_PORTS = frozenset({53, 1900, 5353})


def parse_tls_ports(raw: str | None, default: tuple[int, ...] = (47984, 47990)) -> tuple[int, ...]:
    """Parse literal decimal TLS ports from the environment."""

    if raw is None:
        return default
    ports: list[int] = []
    for item in raw.split(","):
        try:
            port = int(item.strip(), 10)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            ports.append(port)
    return tuple(dict.fromkeys(ports)) or default


@dataclasses.dataclass(frozen=True)
class HostProfile:
    """The non-secret parts of one host entry in Moonlight.conf."""

    index: int
    name: str
    manual_address: str = ""
    local_address: str = ""
    server_certificate: str = ""
    apps: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Settings:
    """User-level policy; Moonlight's existing settings remain untouched."""

    host_profile_name: str = "Mac - Extended Displays"
    config_path: str = (
        "~/.var/app/com.moonlight_stream.Moonlight/config/"
        "Moonlight Game Streaming Project/Moonlight.conf"
    )
    company_networks: tuple[str, ...] = ("192.168.8.0/24",)
    trusted_ssids: tuple[str, ...] = ("JKS_2.4G", "JKS_5G")
    moonlight_app_id: str = "com.moonlight_stream.Moonlight"
    poll_seconds: float = 5.0
    idle_disconnect_seconds: float = 30.0
    backoff_base_seconds: int = 5
    backoff_max_seconds: int = 120
    startup_grace_seconds: float = 15.0
    startup_timeout_seconds: float = 45.0
    health_absence_grace_seconds: float = 5.0
    tls_ports: tuple[int, ...] = (47984, 47990)
    command_timeout_seconds: float = 20.0

    @classmethod
    def from_environment(cls) -> "Settings":
        """Read optional policy overrides from the user service environment."""

        def text(name: str, default: str) -> str:
            return os.environ.get(name, default).strip()

        def values(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return tuple(item.strip() for item in raw.split(",") if item.strip())

        def number(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, str(default)))
            except ValueError:
                return default

        def integer(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, str(default)))
            except ValueError:
                return default

        return cls(
            host_profile_name=text("MOONLIGHT_HOST_PROFILE", cls.host_profile_name),
            config_path=text("MOONLIGHT_CONFIG", cls.config_path),
            company_networks=values("MOONLIGHT_COMPANY_NETWORKS", cls.company_networks),
            trusted_ssids=values("MOONLIGHT_TRUSTED_SSIDS", cls.trusted_ssids),
            moonlight_app_id=text("MOONLIGHT_APP_ID", cls.moonlight_app_id),
            poll_seconds=max(1.0, number("MOONLIGHT_POLL_SECONDS", cls.poll_seconds)),
            idle_disconnect_seconds=max(
                1.0, number("MOONLIGHT_IDLE_DISCONNECT_SECONDS", cls.idle_disconnect_seconds)
            ),
            backoff_base_seconds=max(1, integer("MOONLIGHT_BACKOFF_BASE_SECONDS", cls.backoff_base_seconds)),
            backoff_max_seconds=max(1, integer("MOONLIGHT_BACKOFF_MAX_SECONDS", cls.backoff_max_seconds)),
            startup_grace_seconds=max(
                1.0, number("MOONLIGHT_STARTUP_GRACE_SECONDS", cls.startup_grace_seconds)
            ),
            startup_timeout_seconds=max(
                1.0, number("MOONLIGHT_STARTUP_TIMEOUT_SECONDS", cls.startup_timeout_seconds)
            ),
            health_absence_grace_seconds=max(
                1.0,
                number(
                    "MOONLIGHT_HEALTH_ABSENCE_GRACE_SECONDS",
                    cls.health_absence_grace_seconds,
                ),
            ),
            tls_ports=parse_tls_ports(os.environ.get("MOONLIGHT_TLS_PORTS"), cls.tls_ports),
            command_timeout_seconds=max(
                1.0, number("MOONLIGHT_COMMAND_TIMEOUT_SECONDS", cls.command_timeout_seconds)
            ),
        )


def select_host_profile(profiles: Iterable[HostProfile], name: str) -> HostProfile | None:
    """Find the paired profile by its visible Moonlight host name."""

    wanted = name.strip().casefold()
    return next((profile for profile in profiles if profile.name.casefold() == wanted), None)


def qt_value(value: str) -> str:
    """Decode the small Qt INI subset used by Moonlight.conf."""

    value = value.strip()
    if value.startswith("@ByteArray(") and value.endswith(")"):
        value = value[len("@ByteArray(") : -1]
    def decode_escape(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "n":
            return "\n"
        if token == "r":
            return "\r"
        if token == "t":
            return "\t"
        if token == "\\":
            return "\\"
        return chr(int(token[1:], 16))

    return re.sub(r"\\(n|r|t|\\|x[0-9A-Fa-f]{2})", decode_escape, value)


def parse_moonlight_hosts(config_text: str) -> list[HostProfile]:
    """Parse host entries without loading or returning the client's private key."""

    entries: dict[int, dict[str, object]] = {}
    in_hosts = False
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if line.startswith("["):
            in_hosts = line == "[hosts]"
            continue
        if not in_hosts or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        match = re.fullmatch(r"(\d+)\\(.+)", key)
        if not match:
            continue
        index = int(match.group(1))
        field = match.group(2)
        entry = entries.setdefault(index, {"apps": {}})
        if field in {"hostname", "manualaddress", "localaddress", "srvcert"}:
            entry[field] = qt_value(raw_value)
            continue
        app_match = re.fullmatch(r"apps\\(\d+)\\name", field)
        if app_match:
            apps = entry.setdefault("apps", {})
            assert isinstance(apps, dict)
            apps[int(app_match.group(1))] = qt_value(raw_value)

    profiles: list[HostProfile] = []
    for index in sorted(entries):
        entry = entries[index]
        apps_value = entry.get("apps", {})
        apps = tuple(name for _, name in sorted(apps_value.items())) if isinstance(apps_value, dict) else ()
        profiles.append(
            HostProfile(
                index=index,
                name=str(entry.get("hostname", "")),
                manual_address=str(entry.get("manualaddress", "")),
                local_address=str(entry.get("localaddress", "")),
                server_certificate=str(entry.get("srvcert", "")),
                apps=apps,
            )
        )
    return profiles


def resolve_ipv4(address_or_name: str) -> tuple[str, ...]:
    """Resolve a literal or mDNS name, keeping only unique IPv4 addresses."""

    try:
        ipaddress.IPv4Address(address_or_name)
        return (address_or_name,)
    except ValueError:
        pass
    try:
        records = socket.getaddrinfo(address_or_name, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return ()
    return tuple(dict.fromkeys(record[4][0] for record in records))


def host_candidates(
    profile: HostProfile,
    networks: Iterable[str],
    *,
    resolve=resolve_ipv4,
) -> tuple[str, ...]:
    """Return current mDNS addresses followed by the stored local address."""

    allowed = tuple(ipaddress.ip_network(network, strict=False) for network in networks)
    candidates: list[str] = []
    for value in (profile.manual_address, profile.local_address):
        if not value:
            continue
        values = resolve(value) if value == profile.manual_address else (value,)
        for candidate in values:
            try:
                address = ipaddress.IPv4Address(candidate)
            except ValueError:
                continue
            if any(address in network for network in allowed) and candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def certificate_matches(pem_certificate: str, peer_der: bytes) -> bool:
    """Compare a TLS peer certificate with Moonlight's stored host certificate."""

    try:
        pinned_der = ssl.PEM_cert_to_DER_cert(pem_certificate)
    except (ssl.SSLError, ValueError, TypeError):
        return False
    return hmac.compare_digest(hashlib.sha256(pinned_der).digest(), hashlib.sha256(peer_der).digest())


def peer_certificate_der(address: str, port: int, *, timeout: float = 2.0) -> bytes | None:
    """Read a server certificate without trusting it as a CA."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((address, port), timeout=timeout) as connection:
            with context.wrap_socket(connection, server_hostname=address) as tls_connection:
                return tls_connection.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None


def verify_pinned_host(
    address: str,
    pem_certificate: str,
    *,
    ports: Iterable[int] = (47984, 47990),
    timeout: float = 2.0,
    peer_certificate=peer_certificate_der,
) -> bool:
    """Require a pinned Sunshine/Vibepollo certificate before using an address."""

    if not pem_certificate:
        return False
    for port in ports:
        peer_der = peer_certificate(address, port, timeout=timeout)
        if peer_der is not None and certificate_matches(pem_certificate, peer_der):
            return True
    return False


def parse_ipv4_addresses(ip_output: str) -> dict[str, tuple[ipaddress.IPv4Interface, ...]]:
    """Return global IPv4 interfaces keyed by Linux device name."""

    result: dict[str, list[ipaddress.IPv4Interface]] = {}
    for line in ip_output.splitlines():
        tokens = line.split()
        if "inet" in tokens:
            position = tokens.index("inet")
            if position + 1 >= len(tokens):
                continue
            address = tokens[position + 1]
            device = tokens[1].rstrip(":") if len(tokens) > 1 and tokens[0].endswith(":") else ""
        elif len(tokens) >= 3 and tokens[1] == "dev":
            address = tokens[0]
            device = tokens[2]
        else:
            continue
        try:
            parsed = ipaddress.ip_interface(address)
        except ValueError:
            continue
        if isinstance(parsed, ipaddress.IPv4Interface):
            result.setdefault(device, []).append(parsed)
    return {device: tuple(values) for device, values in result.items()}


def parse_nmcli_devices(nmcli_output: str) -> list[tuple[str, str, str, str]]:
    """Parse ``nmcli -t --escape no -f DEVICE,TYPE,STATE,CONNECTION device``."""

    devices = []
    for line in nmcli_output.splitlines():
        if not line.strip() or line.startswith("DEVICE:"):
            continue
        fields = line.split(":", 3)
        if len(fields) != 4:
            continue
        devices.append(tuple(field.strip() for field in fields))
    return devices


def _in_networks(address: ipaddress.IPv4Interface, networks: Iterable[str]) -> bool:
    return any(address.ip in ipaddress.ip_network(network, strict=False) for network in networks)


def company_network_present(
    ip_output: str,
    nmcli_output: str,
    *,
    networks: Iterable[str],
    trusted_ssids: Iterable[str],
) -> bool:
    """Return true only for an active trusted connection on a company subnet.

    Ethernet is accepted by subnet. Wi-Fi additionally needs an allow-listed
    SSID, which prevents the similarly addressed guest SSID from triggering a
    stream. If NetworkManager is unavailable, the subnet check remains a safe
    fallback for wired/headless environments.
    """

    addresses = parse_ipv4_addresses(ip_output)
    trusted = {ssid.strip() for ssid in trusted_ssids if ssid.strip()}
    devices = parse_nmcli_devices(nmcli_output)
    if not devices:
        return any(_in_networks(address, networks) for values in addresses.values() for address in values)

    for device, kind, state, connection in devices:
        if not state.lower().startswith("connected"):
            continue
        device_addresses = addresses.get(device, ())
        if not any(_in_networks(address, networks) for address in device_addresses):
            continue
        normalized_kind = kind.lower()
        if normalized_kind in {"ethernet", "802-3-ethernet"}:
            return True
        if normalized_kind in {"wifi", "802-11-wireless", "wireless"} and connection in trusted:
            return True
    return False


def backoff_seconds(attempt: int, *, base: int = 5, maximum: int = 120) -> int:
    """Exponential retry delay with a fixed upper bound."""

    return min(maximum, base * (2 ** max(0, attempt)))


def select_stream_app(apps: Iterable[str]) -> str | None:
    """Choose the fork's Remote Monitor entry, with its current Resume fallback."""

    names = {app.strip() for app in apps}
    if "Remote Monitor" in names:
        return "Remote Monitor"
    if "Resume" in names:
        return "Resume"
    return None


def existing_stream_pids(*, proc_root: str = "/proc", current_pid: int | None = None) -> tuple[int, ...]:
    """Find Moonlight stream processes, including Flatpak's bwrap child."""

    current_pid = os.getpid() if current_pid is None else current_pid
    found: list[int] = []
    try:
        entries = os.scandir(proc_root)
    except OSError:
        return ()
    with entries:
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == current_pid:
                continue
            try:
                raw_command = (Path(entry.path) / "cmdline").read_bytes()
            except OSError:
                continue
            command = [token.decode(errors="replace") for token in raw_command.split(b"\0") if token]
            lowered = [token.lower() for token in command]
            has_moonlight = any("moonlight" in token for token in lowered)
            if has_moonlight and "stream" in lowered:
                found.append(int(entry.name))
    return tuple(sorted(found))


def descendant_pids(pid: int, *, proc_root: str = "/proc") -> tuple[int, ...]:
    """Return descendant process IDs visible through Linux procfs."""

    pending = [pid]
    found: set[int] = set()
    while pending:
        parent = pending.pop()
        if parent in found:
            continue
        found.add(parent)
        children_path = Path(proc_root) / str(parent) / "task" / str(parent) / "children"
        try:
            children = tuple(int(value) for value in children_path.read_text().split())
        except (OSError, ValueError):
            continue
        pending.extend(child for child in children if child not in found)
    found.discard(pid)
    return tuple(sorted(found))


def _local_udp_endpoint(
    endpoint: str,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int] | None:
    """Parse the numeric local endpoint column emitted by ``ss -n``."""

    if endpoint.startswith("["):
        close = endpoint.rfind("]:")
        if close < 0:
            return None
        host = endpoint[1:close]
        port_text = endpoint[close + 2 :]
    else:
        host, separator, port_text = endpoint.rpartition(":")
        if not separator:
            return None
    host = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
        port = int(port_text)
    except ValueError:
        return None
    return address, port


def owned_udp_socket_count(
    ss_output: str,
    pids: Iterable[int],
    *,
    networks: Iterable[str] | None = None,
) -> int:
    """Count owned, routable UDP rows, excluding common discovery sockets."""

    owned = {pid for pid in pids if isinstance(pid, int) and pid > 0}
    if not owned:
        return 0
    allowed = tuple(ipaddress.ip_network(network, strict=False) for network in networks or ())
    count = 0
    for line in ss_output.splitlines():
        line_pids = {int(value) for value in re.findall(r"pid=(\d+)", line)}
        if not line_pids & owned:
            continue
        fields = line.split()
        if len(fields) < 5:
            continue
        endpoint = _local_udp_endpoint(fields[3])
        if endpoint is None:
            continue
        address, port = endpoint
        if port in DISCOVERY_UDP_PORTS or address.is_unspecified or address.is_loopback:
            continue
        if allowed and not any(address in network for network in allowed):
            continue
        count += 1
    return count


class ReconnectController:
    """Small state machine for presence, retry, and stream ownership."""

    def __init__(
        self,
        settings: Settings,
        *,
        command_runner=subprocess.run,
        process_factory=subprocess.Popen,
        monotonic=time.monotonic,
        logger: logging.Logger | None = None,
        proc_root: str = "/proc",
    ) -> None:
        self.settings = settings
        self.command_runner = command_runner
        self.process_factory = process_factory
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger("moonlight-reconnect")
        self.proc_root = proc_root
        self.process = None
        self.process_started_at: float | None = None
        self.active_profile: HostProfile | None = None
        self.active_address: str | None = None
        self.stream_ready = False
        self.udp_absent_since: float | None = None
        self.health_failed_since: float | None = None
        self.absent_since: float | None = None
        self.manual_hold = False
        self.retry_attempt = 0
        self.next_retry_at = 0.0
        self._stop = False

    def network_present(self) -> bool:
        """Read active addresses and NetworkManager state for this iteration."""

        ip_result = self._run(["ip", "-o", "-4", "addr", "show", "scope", "global"], timeout=3)
        nmcli_result = self._run(
            [
                "nmcli",
                "-t",
                "--escape",
                "no",
                "-f",
                "DEVICE,TYPE,STATE,CONNECTION",
                "device",
                "status",
            ],
            timeout=3,
        )
        return company_network_present(
            ip_result.stdout if ip_result else "",
            nmcli_result.stdout if nmcli_result else "",
            networks=self.settings.company_networks,
            trusted_ssids=self.settings.trusted_ssids,
        )

    def step(self) -> None:
        """Run one poll iteration. Safe to call from tests or a service loop."""

        now = self.monotonic()
        present = self.network_present()
        self._observe_process(present, now)

        if not present:
            if self.absent_since is None:
                self.absent_since = now
                self.manual_hold = False
                self.retry_attempt = 0
                self.next_retry_at = now
                self.logger.info("company network absent; waiting before reconnect")
            if self.process is not None and now - self.absent_since >= self.settings.idle_disconnect_seconds:
                self.logger.info("company network absent for %.0fs; stopping owned stream", now - self.absent_since)
                self.stop_owned_stream()
            return

        if self.absent_since is not None:
            self.absent_since = None
            self.manual_hold = False
            self.retry_attempt = 0
            self.next_retry_at = now
            self.logger.info("company network present; reconnecting")

        if self.process is not None:
            self._check_stream_health(now)
            return
        if self.manual_hold or now < self.next_retry_at:
            return
        if existing_stream_pids(proc_root=self.proc_root):
            self.manual_hold = True
            self.logger.info("Moonlight stream already exists; leaving it alone")
            return
        if self.attempt_start():
            self.retry_attempt = 0
            self.next_retry_at = now
        else:
            self.retry_attempt += 1
            delay = backoff_seconds(
                self.retry_attempt - 1,
                base=self.settings.backoff_base_seconds,
                maximum=self.settings.backoff_max_seconds,
            )
            self.next_retry_at = now + delay
            self.logger.info("reconnect attempt failed; retrying in %ss", delay)

    def _observe_process(self, present: bool, now: float) -> None:
        if self.process is None:
            return
        exit_code = self.process.poll()
        if exit_code is None:
            return
        started_at = self.process_started_at
        self._clear_process_state()
        if exit_code == 0 and present and (
            started_at is None or now - started_at >= self.settings.startup_grace_seconds
        ):
            self.manual_hold = True
            self.logger.info("Moonlight exited cleanly; holding until network transition or service restart")
            return
        self._schedule_retry(now, "Moonlight exited with status %s", exit_code)

    def _schedule_retry(self, now: float, message: str, *message_args: object) -> None:
        self.retry_attempt += 1
        delay = backoff_seconds(
            self.retry_attempt - 1,
            base=self.settings.backoff_base_seconds,
            maximum=self.settings.backoff_max_seconds,
        )
        self.next_retry_at = now + delay
        rendered = message % message_args if message_args else message
        self.logger.info("%s; retrying in %ss", rendered, delay)

    def _clear_process_state(self) -> None:
        self.process = None
        self.process_started_at = None
        self.active_profile = None
        self.active_address = None
        self.stream_ready = False
        self.udp_absent_since = None
        self.health_failed_since = None

    def _run(self, command: list[str], *, timeout: float):
        try:
            return self.command_runner(command, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return None

    def _owned_udp_socket_count(self) -> int | None:
        """Return owned Moonlight UDP rows, or None when ``ss`` is unavailable."""

        process_id = getattr(self.process, "pid", None)
        if not isinstance(process_id, int) or process_id <= 0:
            return None
        result = self._run(["ss", "-H", "-u", "-a", "-n", "-p"], timeout=2.0)
        if result is None or result.returncode != 0:
            return None
        pids = (process_id, *descendant_pids(process_id, proc_root=self.proc_root))
        return owned_udp_socket_count(result.stdout, pids, networks=self.settings.company_networks)

    def _check_stream_health(self, now: float) -> None:
        """Recover a stuck or disconnected stream that left its GUI process alive."""

        if self.process is None:
            return

        socket_count = self._owned_udp_socket_count()
        if not self.stream_ready and socket_count is not None and socket_count >= MIN_STREAM_UDP_SOCKETS:
            self.stream_ready = True
            self.logger.info("Moonlight stream ready (%s owned UDP sockets)", socket_count)

        if (
            not self.stream_ready
            and socket_count is not None
            and socket_count < MIN_STREAM_UDP_SOCKETS
            and self.process_started_at is not None
            and now - self.process_started_at >= self.settings.startup_timeout_seconds
        ):
            self.logger.info(
                "Moonlight stream produced no UDP session within %.0fs; restarting owned process",
                self.settings.startup_timeout_seconds,
            )
            self.stop_owned_stream()
            self._schedule_retry(now, "Moonlight startup watchdog fired")
            return

        if self.stream_ready:
            if socket_count is None:
                self.udp_absent_since = None
            elif socket_count == 0:
                if self.udp_absent_since is None:
                    self.udp_absent_since = now
                    self.logger.info("owned Moonlight UDP session disappeared; waiting before stopping")
                elif now - self.udp_absent_since >= self.settings.health_absence_grace_seconds:
                    self.logger.info(
                        "owned Moonlight UDP session absent for %.0fs; restarting stream",
                        now - self.udp_absent_since,
                    )
                    self.stop_owned_stream()
                    self._schedule_retry(now, "Moonlight UDP session watchdog fired")
                    return
            else:
                self.udp_absent_since = None

        if self.active_profile is None or self.active_address is None:
            return
        if verify_pinned_host(
            self.active_address,
            self.active_profile.server_certificate,
            ports=self.settings.tls_ports,
        ):
            self.health_failed_since = None
            return
        if self.health_failed_since is None:
            self.health_failed_since = now
            self.logger.info("paired host health check failed; waiting before stopping stream")
            return
        if now - self.health_failed_since < self.settings.health_absence_grace_seconds:
            return
        self.logger.info(
            "paired host unavailable for %.0fs; stopping owned stream",
            now - self.health_failed_since,
        )
        self.stop_owned_stream()
        self._schedule_retry(now, "Moonlight host health watchdog fired")

    def _profile(self) -> HostProfile | None:
        configured_path = Path(os.path.expanduser(self.settings.config_path))
        paths = [configured_path]
        default_path = Path(os.path.expanduser(Settings.config_path))
        native_path = Path.home() / ".config/Moonlight Game Streaming Project/Moonlight.conf"
        if configured_path == default_path:
            paths.append(native_path)
        first_profile: HostProfile | None = None
        for path in paths:
            try:
                config = path.read_text(encoding="utf-8")
            except OSError:
                continue
            profile = select_host_profile(parse_moonlight_hosts(config), self.settings.host_profile_name)
            if profile is None:
                continue
            first_profile = first_profile or profile
            if profile.server_certificate:
                return profile
        if first_profile is None:
            self.logger.warning("paired host profile %r was not found", self.settings.host_profile_name)
        else:
            self.logger.warning("paired host profile has no pinned server certificate")
        return first_profile

    def _list_apps(self, address: str) -> tuple[str, ...]:
        result = self._run(
            ["flatpak", "run", self.settings.moonlight_app_id, "list", address],
            timeout=self.settings.command_timeout_seconds,
        )
        if result is None or result.returncode != 0:
            return ()
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def attempt_start(self) -> bool:
        """Find and verify the paired host, then launch exactly one stream."""

        profile = self._profile()
        if profile is None:
            return False
        for address in host_candidates(profile, self.settings.company_networks):
            if not verify_pinned_host(address, profile.server_certificate, ports=self.settings.tls_ports):
                continue
            app = select_stream_app(self._list_apps(address))
            if app is None:
                continue
            command = [
                "flatpak",
                "run",
                self.settings.moonlight_app_id,
                "--video-codec",
                "H.264",
                "--video-decoder",
                "hardware",
                "--resolution",
                "2160x1440",
                "--fps",
                "30",
                "--bitrate",
                "12000",
                "--no-yuv444",
                "--frame-pacing",
                "--vsync",
                "--no-game-optimization",
                "--display-mode",
                "fullscreen",
                "stream",
                address,
                app,
            ]
            environment = os.environ.copy()
            environment["VIBEPOLLO_MOONLIGHT_RECONNECT_OWNER"] = "1"
            try:
                self.process = self.process_factory(command, env=environment, start_new_session=True)
            except OSError:
                return False
            self.process_started_at = self.monotonic()
            self.active_profile = profile
            self.active_address = address
            self.stream_ready = False
            self.udp_absent_since = None
            self.health_failed_since = None
            self.logger.info("started %s on paired host %s", app, address)
            return True
        return False

    def stop_owned_stream(self) -> None:
        if self.process is None:
            return
        process = self.process
        self._clear_process_state()
        try:
            self._signal_owned_process(process, signal.SIGTERM, "terminate")
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            try:
                self._signal_owned_process(process, signal.SIGKILL, "kill")
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError, TimeoutError):
                pass

    @staticmethod
    def _signal_owned_process(process, signum: int, fallback: str) -> None:
        """Signal only the session created for our Flatpak launcher."""

        process_id = getattr(process, "pid", None)
        if isinstance(process_id, int) and process_id > 0:
            try:
                os.killpg(process_id, signum)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        getattr(process, fallback)()

    def run_forever(self) -> None:
        """Run until SIGTERM/SIGINT calls ``stop``."""

        while not self._stop:
            self.step()
            time.sleep(self.settings.poll_seconds)

    def stop(self) -> None:
        self._stop = True
        self.stop_owned_stream()


def acquire_instance_lock(path: str):
    """Hold an advisory lock so a second autostart cannot duplicate streams."""

    lock_path = Path(os.path.expanduser(path))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock-file",
        default=os.environ.get("MOONLIGHT_LOCK_FILE", "~/.cache/vibepollo/moonlight-reconnect.lock"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lock = acquire_instance_lock(args.lock_file)
    if lock is None:
        logging.info("another reconnect helper is already running")
        return 0
    controller = ReconnectController(Settings.from_environment())
    signal.signal(signal.SIGTERM, lambda _signal, _frame: controller.stop())
    signal.signal(signal.SIGINT, lambda _signal, _frame: controller.stop())
    try:
        controller.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
