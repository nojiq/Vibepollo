import importlib.util
import pathlib
import base64
import tempfile
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "moonlight_reconnect.py"
SPEC = importlib.util.spec_from_file_location("moonlight_reconnect", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["moonlight_reconnect"] = module
SPEC.loader.exec_module(module)


class HostConfigTests(unittest.TestCase):
    def test_reads_selected_host_without_touching_general_client_key(self):
        config = r'''[General]
key=@ByteArray(THIS IS NOT A HOST SECRET)

[hosts]
1\hostname=Mac - Extended Displays
1\manualaddress=helpdesks-macbook-air-2.local
1\localaddress=192.168.8.150
1\srvcert=@ByteArray(-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----\n)
1\apps\1\name=Resume
1\apps\size=1
size=1
'''

        hosts = module.parse_moonlight_hosts(config)

        self.assertEqual(len(hosts), 1)
        host = hosts[0]
        self.assertEqual(host.name, "Mac - Extended Displays")
        self.assertEqual(host.manual_address, "helpdesks-macbook-air-2.local")
        self.assertEqual(host.local_address, "192.168.8.150")
        self.assertIn("BEGIN CERTIFICATE", host.server_certificate)
        self.assertEqual(host.apps, ("Resume",))

    def test_qt_bytearray_unescapes_newlines_and_parentheses(self):
        value = r"@ByteArray(one\ntwo\\three)"

        self.assertEqual(module.qt_value(value), "one\ntwo\\three")

    def test_selects_host_profile_by_name(self):
        profiles = [
            module.HostProfile(index=1, name="Other Mac"),
            module.HostProfile(index=2, name="Mac - Extended Displays"),
        ]

        self.assertEqual(module.select_host_profile(profiles, "mac - extended displays").index, 2)
        self.assertIsNone(module.select_host_profile(profiles, "missing"))

    def test_candidates_use_current_mdns_ip_before_stale_profile_ip(self):
        profile = module.HostProfile(
            index=1,
            name="Mac - Extended Displays",
            manual_address="mac.local",
            local_address="192.168.8.150",
        )

        def resolve(name):
            self.assertEqual(name, "mac.local")
            return ("192.168.8.200",)

        self.assertEqual(
            module.host_candidates(profile, ("192.168.8.0/24",), resolve=resolve),
            ("192.168.8.200", "192.168.8.150"),
        )

    def test_candidates_reject_outside_company_network(self):
        profile = module.HostProfile(
            index=1,
            name="Mac - Extended Displays",
            manual_address="mac.local",
            local_address="10.0.0.5",
        )

        self.assertEqual(
            module.host_candidates(
                profile,
                ("192.168.8.0/24",),
                resolve=lambda _name: ("10.0.0.6",),
            ),
            (),
        )

    def test_certificate_match_uses_sha256_of_peer_der(self):
        peer_der = b"test-peer-certificate"
        pem = "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(peer_der).decode() + "\n-----END CERTIFICATE-----\n"

        self.assertTrue(module.certificate_matches(pem, peer_der))
        self.assertFalse(module.certificate_matches(pem, b"different"))

    def test_pinned_host_accepts_matching_peer_on_any_configured_tls_port(self):
        peer_der = b"test-peer-certificate"
        pem = "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(peer_der).decode() + "\n-----END CERTIFICATE-----\n"

        def peer_certificate(_address, port, *, timeout):
            return peer_der if port == 47990 else None

        self.assertTrue(
            module.verify_pinned_host(
                "192.168.8.150",
                pem,
                ports=(47984, 47990),
                peer_certificate=peer_certificate,
            )
        )

    def test_pinned_host_rejects_mismatch_and_missing_pin(self):
        peer_certificate = lambda _address, _port, *, timeout: b"other"

        self.assertFalse(
            module.verify_pinned_host(
                "192.168.8.150",
                "",
                peer_certificate=peer_certificate,
            )
        )


class NetworkTests(unittest.TestCase):
    def test_company_wifi_requires_trusted_ssid(self):
        devices = """DEVICE:TYPE:STATE:CONNECTION
wlp2s0:wifi:connected:JKS_Guest_2.4G
"""
        addresses = "192.168.8.134/24 dev wlp2s0 scope global"

        self.assertFalse(
            module.company_network_present(
                addresses,
                devices,
                networks=("192.168.8.0/24",),
                trusted_ssids=("JKS_2.4G", "JKS_5G"),
            )
        )

    def test_company_ethernet_is_allowed_on_company_subnet(self):
        devices = """DEVICE:TYPE:STATE:CONNECTION
enx00e04c4f8220:ethernet:connected:Wired connection 1
"""
        addresses = "192.168.8.134/24 dev enx00e04c4f8220 scope global"

        self.assertTrue(
            module.company_network_present(
                addresses,
                devices,
                networks=("192.168.8.0/24",),
                trusted_ssids=("JKS_2.4G", "JKS_5G"),
            )
        )

    def test_wifi_must_have_company_address_on_same_device(self):
        devices = """DEVICE:TYPE:STATE:CONNECTION
wlp2s0:wifi:connected:JKS_5G
"""
        addresses = "10.0.0.42/24 dev wlp2s0 scope global"

        self.assertFalse(
            module.company_network_present(
                addresses,
                devices,
                networks=("192.168.8.0/24",),
                trusted_ssids=("JKS_2.4G", "JKS_5G"),
            )
        )


class RetryTests(unittest.TestCase):
    def test_backoff_is_bounded_and_resets(self):
        values = [module.backoff_seconds(i, base=2, maximum=30) for i in range(8)]

        self.assertEqual(values, [2, 4, 8, 16, 30, 30, 30, 30])
        self.assertEqual(module.backoff_seconds(0), 5)

    def test_app_selection_prefers_remote_monitor_then_resume(self):
        self.assertEqual(module.select_stream_app(("Resume", "Remote Monitor")), "Remote Monitor")
        self.assertEqual(module.select_stream_app(("Disconnect Monitor", "Resume")), "Resume")
        self.assertIsNone(module.select_stream_app(("Desktop",)))


class ProcessAndControllerTests(unittest.TestCase):
    def test_existing_stream_scan_finds_flatpak_moonlight_stream_once(self):
        with tempfile.TemporaryDirectory() as proc_root:
            for pid, command in {
                "101": b"bwrap\0--\0moonlight\0stream\0" + b"192.168.8.150\0Resume\0",
                "102": b"moonlight\0list\0" + b"192.168.8.150\0",
                "103": b"other\0stream\0",
            }.items():
                pid_dir = pathlib.Path(proc_root) / pid
                pid_dir.mkdir()
                (pid_dir / "cmdline").write_bytes(command)

            self.assertEqual(module.existing_stream_pids(proc_root=proc_root, current_pid=999), (101,))

    def test_disconnect_terminates_owned_stream_after_grace_period(self):
        class FakeProcess:
            def __init__(self):
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                return 0

        class FakeController(module.ReconnectController):
            def __init__(self):
                settings = module.Settings(idle_disconnect_seconds=30)
                super().__init__(settings, monotonic=lambda: self.now)
                self.present = True
                self.now = 0

            def network_present(self):
                return self.present

            def attempt_start(self):
                return False

        controller = FakeController()
        process = FakeProcess()
        controller.process = process
        controller.present = False
        controller.step()
        controller.now = 29
        controller.step()
        self.assertFalse(process.terminated)
        controller.now = 30
        controller.step()
        self.assertTrue(process.terminated)

    def test_clean_manual_quit_is_held_until_network_transition(self):
        class FakeProcess:
            def poll(self):
                return 0

        class FakeController(module.ReconnectController):
            def __init__(self):
                super().__init__(module.Settings(), monotonic=lambda: self.now)
                self.now = 0
                self.present = True
                self.starts = 0

            def network_present(self):
                return self.present

            def attempt_start(self):
                self.starts += 1
                return False

        controller = FakeController()
        controller.process = FakeProcess()
        controller.step()
        controller.step()
        self.assertEqual(controller.starts, 0)
        controller.present = False
        controller.step()
        controller.present = True
        controller.step()
        controller.step()
        self.assertEqual(controller.starts, 1)


if __name__ == "__main__":
    unittest.main()
