"""Preserve the saved AP profile around the official USB/Wi-Fi uplink lifecycle."""
import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import phone_uplink
from phone_uplink import PhoneUplink
from settings_page import SettingsPage


class UpgradeRadioUplinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / 'uplink-hotspot.json').write_text(json.dumps({
            'ssid': 'Phone', 'passphrase': 'synthetic-phone-password',
        }), encoding='utf-8')
        self.p = Mock()
        self.p.concurrent_ap.return_value = 1
        self.p.add_phone_ap.return_value = True
        self.p.start_ap.return_value = True
        self.p.ap_ready.return_value = True
        self.p.ap_channel_problem.return_value = None
        self.p.phone_hotspot_frequency.return_value = None
        self.p.phone_link.return_value = {'connected': False, 'frequency': None}
        self.p.phone_station_address.return_value = None
        self.p.uplink_gateway.return_value = '192.0.2.1'
        self.p.default_route_devices.return_value = []
        self.p.wired_carrier.return_value = False
        self.p.local_addresses.return_value = []
        self.p.interface, self.p.ap_interface = 'wlan0', 'ap0'
        self.p.run = self.state
        self.p.phone_resolv = self.state / 'dns/phone-resolv.conf'
        self.profile = None  # channel, width, whether the AP is restricted to 802.11n
        self.p.configure_hotspot.side_effect = self.configure_hotspot
        self.p.n_only_ap.side_effect = self.n_only_ap
        self.now = 0
        self.pending = False
        self.receiver = Mock()
        self.status = {}

    def configure_hotspot(self, channel, width, ssid, passphrase):
        self.profile = (channel, width, False)

    def n_only_ap(self):
        self.profile = (*self.profile[:2], True)

    def controller(self, share=True, **kwargs):
        return PhoneUplink(
            self.p, self.state, ipaddress.ip_interface('100.96.0.1/24'),
            {'ssid': 'Box', 'passphrase': 'synthetic-box-password'}, 149, 40,
            Mock(), self.receiver, self.status, say=lambda _: None,
            clock=lambda: self.now, share=share, **kwargs,
        )

    def due_tick(self, uplink):
        self.now = max(self.now, uplink.next_try)
        uplink.tick()

    def connect_hotspot(self, uplink, frequency):
        self.p.phone_hotspot_frequency.return_value = frequency
        self.due_tick(uplink)
        self.assertTrue(uplink.station)
        self.p.phone_link.return_value = {'connected': True, 'frequency': frequency}
        self.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        uplink.tick()
        self.assertEqual(uplink.phase, 'online')

    def assert_profile(self, uplink, channel, width, following):
        self.assertEqual(self.profile, (channel, width, following))
        self.assertEqual((uplink.channel, uplink.width, uplink.following),
                         (channel, width, following))

    def test_usb_boot_keeps_saved_profile_with_or_without_carrier_and_memory(self):
        for routes, carrier, remembered in (
                ([], False, None), ([], False, 5180),
                ([], True, 5180), (['usb0'], False, 5180)):
            with self.subTest(routes=routes, carrier=carrier, remembered=remembered):
                self.p.default_route_devices.return_value = routes
                self.p.wired_carrier.return_value = carrier
                uplink = self.controller()
                if remembered:
                    uplink.search.remember(remembered)
                self.assertTrue(uplink.start())
                self.assert_profile(uplink, 149, 40, False)
                self.assertEqual(uplink.default_width, 40)
                self.p.phone_hotspot_frequency.assert_not_called()

    def test_pure_wifi_keeps_official_twenty_mhz_following_profile(self):
        self.p.phone_hotspot_frequency.return_value = 5180
        uplink = self.controller(share=False)
        self.assertTrue(uplink.start())
        self.assert_profile(uplink, 36, 20, True)

    def test_ap_helpers_keep_official_defaults_when_optional_arguments_are_omitted(self):
        uplink = self.controller()
        self.assertTrue(uplink.start())
        uplink.configure_ap(36)
        self.assert_profile(uplink, 36, 20, True)
        self.assertTrue(uplink.rebuild_ap(149))
        self.assert_profile(uplink, 149, 20, True)

    def test_same_channel_wifi_fallback_changes_width_and_disables_vht(self):
        uplink = self.controller()
        self.assertTrue(uplink.start())
        self.connect_hotspot(uplink, 5745)
        self.assert_profile(uplink, 149, 20, True)
        self.assertGreater(self.receiver.call_count, 1,
                           'the 40-to-20 MHz change must apply to the running AP')

    def test_usb_return_restores_saved_profile_from_either_five_ghz_channel(self):
        for frequency in (5180, 5745):
            with self.subTest(frequency=frequency):
                self.p.default_route_devices.return_value = []
                uplink = self.controller()
                self.assertTrue(uplink.start())
                self.connect_hotspot(uplink, frequency)
                self.p.default_route_devices.return_value = ['usb0']
                uplink.tick()
                self.assertEqual(uplink.phase, 'wired')
                self.assertFalse(uplink.station)
                self.assert_profile(uplink, 149, 40, False)

    def test_usb_return_also_restores_profile_after_station_already_disconnected(self):
        uplink = self.controller()
        self.assertTrue(uplink.start())
        self.connect_hotspot(uplink, 5180)
        uplink.disconnect_station()
        self.p.default_route_devices.return_value = ['usb0']
        self.due_tick(uplink)
        self.assertEqual(uplink.phase, 'wired')
        self.assert_profile(uplink, 149, 40, False)

    def test_pending_radio_confirmation_defers_search_until_released(self):
        self.pending = True
        uplink = self.controller(radio_pending=lambda: self.pending)
        self.p.phone_hotspot_frequency.return_value = 5180
        self.assertTrue(uplink.start())
        for _ in range(3):
            self.now += 400
            uplink.tick()
        self.p.phone_hotspot_frequency.assert_not_called()
        self.p.start_phone_station.assert_not_called()
        self.assert_profile(uplink, 149, 40, False)
        self.pending = False
        self.due_tick(uplink)
        self.assertTrue(uplink.station)
        self.assert_profile(uplink, 36, 20, True)

    def test_pending_radio_confirmation_also_guards_direct_connect(self):
        uplink = self.controller(radio_pending=lambda: self.pending)
        self.assertTrue(uplink.start())
        self.pending = True
        uplink.connect(5180)
        self.p.start_phone_station.assert_not_called()
        self.assertFalse(uplink.station)
        self.assert_profile(uplink, 149, 40, False)

    def test_pending_does_not_disable_ap_health_recovery_or_change_its_profile(self):
        self.pending = True
        uplink = self.controller(radio_pending=lambda: self.pending)
        self.assertTrue(uplink.start())
        self.p.ap_ready.return_value = False
        self.now += 400
        for _ in range(phone_uplink.AP_READY_MISSES):
            uplink.tick()
        self.assertTrue(uplink.started)
        self.assertTrue(self.status['tesla_lan_stable'])
        self.assert_profile(uplink, 149, 40, False)
        self.assertGreater(self.receiver.call_count, 1)
        self.p.phone_hotspot_frequency.assert_not_called()
        self.p.start_phone_station.assert_not_called()

    def test_failed_wifi_transition_restores_complete_saved_profile(self):
        uplink = self.controller()
        self.assertTrue(uplink.start())
        self.p.start_ap.side_effect = [False, False, True]
        uplink.connect(5180)
        self.assertTrue(uplink.started)
        self.assertTrue(self.status['tesla_lan_stable'])
        self.assertFalse(uplink.station)
        self.assert_profile(uplink, 149, 40, False)
        self.p.start_phone_station.assert_not_called()

    def test_failed_saved_profile_transition_restores_complete_following_profile(self):
        uplink = self.controller()
        self.assertTrue(uplink.start())
        self.connect_hotspot(uplink, 5180)
        self.p.start_ap.side_effect = [False, False, True]
        self.assertFalse(uplink.rebuild_ap(149, width=40, following=False))
        self.assertTrue(uplink.started)
        self.assertTrue(self.status['tesla_lan_stable'])
        self.assert_profile(uplink, 36, 20, True)

    def test_failed_transition_and_rollback_request_full_network_recovery(self):
        uplink = self.controller()
        self.assertTrue(uplink.start())
        self.p.start_ap.side_effect = [False, False, False, False, True]
        with self.assertRaises(phone_uplink.LanRecoveryFailed):
            uplink.connect(5180)
        self.assertFalse(self.status['tesla_lan_stable'])
        self.p.start_phone_station.assert_not_called()
        self.assertEqual(self.p.start_dhcp.call_count, 1,
                         'must not start a plain AP with stale USB forwarding interfaces')


class UpgradeRadioSettingsAvailabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / 'setup.json').write_text(json.dumps({
            'mode': 'box_hotspot', 'done': True, 'box': True,
        }), encoding='utf-8')
        (self.state / 'options.json').write_text(json.dumps({
            'car_internet': 'usb_tether',
        }), encoding='utf-8')
        self.status = {'mode': 'ap', 'uplink': {'phase': 'wired'}}
        self.page = SettingsPage(self.state, lambda: self.status,
                                 lambda _: None, lambda: [])

    def test_usb_wifi_fallback_blocks_independent_radio_changes(self):
        for phase in ('connecting', 'online', 'recovering'):
            with self.subTest(phase=phase):
                self.status['uplink']['phase'] = phase
                self.assertTrue(self.page.radio_reason())

    def test_usb_wired_or_offline_ap_allows_independent_radio_changes(self):
        for phase in ('wired', 'offline'):
            with self.subTest(phase=phase):
                self.status['uplink']['phase'] = phase
                self.assertEqual(self.page.radio_reason(), '')

    def test_pure_wifi_uplink_keeps_radio_changes_disabled_even_when_offline(self):
        (self.state / 'options.json').write_text(json.dumps({
            'car_internet': 'phone_hotspot',
        }), encoding='utf-8')
        self.status['uplink']['phase'] = 'offline'
        self.assertTrue(self.page.radio_reason())


if __name__ == '__main__':
    unittest.main()
