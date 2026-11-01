"""USB-primary AP policy and interface ownership; no radio/firmware emulation.

The fake models the existing Debian USB/phone journals and running LAN services.
Passing these tests does not demonstrate successful client WPA authentication.
"""
import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import phone_uplink
from phone_uplink import PhoneUplink


class FakePlatform:
    def __init__(self, root):
        self.interface = self.ap_interface = 'wlan0'
        self.run = root
        self.phone_resolv = root / 'phone-resolv.conf'
        self.usb_online = True
        self.carrier = True
        self.frequency_found = None
        self.scan_error = None
        self.station_error = None
        self.ap_add_ok = True
        self.ready = True
        self.link = {'connected': False, 'frequency': None}
        self.station_address = None
        self.station_running = False
        self.config = None
        self.services = {}
        self.addresses = {}
        self.forward = 0
        self.phone_journal = None
        self.usb_journal = None
        self.usb_rules = set()
        self.usb_rule_instances = []  # iptables permits duplicate insertions
        self.phone_rules = set()
        self.usb_marker = False
        self.events = []

    def concurrent_ap(self):
        return 1

    def default_route_devices(self):
        return ['usb0'] if self.usb_online else []

    def wired_carrier(self):
        return self.carrier

    def ap_channel_problem(self, frequency):
        return None

    def phone_hotspot_frequency(self, ssid, **kwargs):
        self.events.append(('scan', self.ap_interface))
        if self.scan_error:
            raise self.scan_error
        return self.frequency_found

    def prepare_phone_uplink(self):
        self.cleanup_phone_uplink()
        self.events.append(('prepare_phone', self.ap_interface))
        self.phone_journal = self.forward

    def add_phone_ap(self):
        self.events.append(('add_ap', self.ap_interface))
        if not self.ap_add_ok:
            return False
        self.ap_interface = 'ap0'
        return True

    def cleanup_phone_uplink(self):
        if self.phone_journal is None:
            return
        self.events.append(('cleanup_phone', self.ap_interface))
        self.forward = self.phone_journal
        self.phone_journal = None
        self.phone_rules.clear()
        self.station_running = False
        self.addresses.pop('ap0', None)
        self.addresses.pop(self.interface, None)
        self.ap_interface = self.interface

    def configure_hotspot(self, channel, width, ssid, passphrase):
        self.config = (self.ap_interface, channel, width, False, ssid, passphrase)

    def n_only_ap(self):
        iface, channel, width, _, ssid, passphrase = self.config
        self.config = (iface, channel, width, True, ssid, passphrase)

    def set_ap_address(self, address):
        self.address_interface = self.ap_interface
        self.addresses[self.ap_interface] = address

    def flush_address(self):
        self.events.append(('flush_address', self.interface))
        self.addresses.pop(self.interface, None)
        self.addresses.pop('dpap0', None)
        self.ap_interface = self.interface

    def start_ap(self, wait_for):
        self.services['hostapd'] = self.config
        self.events.append(('start_ap', self.ap_interface))
        return True

    def ap_ready(self):
        return self.ready

    def start_dhcp(self, address, portal, names=(), **kwargs):
        self.services['dnsmasq'] = (self.ap_interface, address, kwargs)

    def launch_receiver(self, channel):
        # The official USB bridge reads this marker at receiver startup.
        if not self.usb_marker:
            raise AssertionError('receiver started without the USB tether marker')
        self.services['receiver'] = (self.ap_interface, channel)
        self.events.append(('start_receiver', self.ap_interface))

    def check_lan_children(self):
        if not all(name in self.services for name in ('receiver', 'hostapd', 'dnsmasq')):
            raise RuntimeError('LAN service missing')

    def stop_child(self, name):
        self.services.pop(name, None)
        if name == 'wpa_supplicant':
            self.station_running = False

    def set_phone_dns(self, gateway):
        self.dns_gateway = gateway

    def enable_phone_nat(self, network):
        if self.phone_journal is None:
            raise RuntimeError('phone NAT without its journal')
        self.phone_rules.add('ap0')
        self.forward = 1

    def begin_usb_tether(self, network):
        self.stop_usb_sharing()
        self.usb_journal = (self.forward, self.ap_interface, network)
        self.usb_marker = True
        self.events.append(('begin_usb', self.ap_interface))

    def finish_usb_tether(self):
        if self.usb_journal is None:
            raise ValueError('USB sharing has not been prepared')
        forward, _, network = self.usb_journal
        self.usb_journal = (forward, self.ap_interface, network)
        self.usb_rules.add(self.ap_interface)
        self.usb_rule_instances.append(self.ap_interface)
        self.forward = 1
        self.usb_marker = True
        self.events.append(('finish_usb', self.ap_interface))

    def enable_usb_tether(self, network):
        self.begin_usb_tether(network)
        self.finish_usb_tether()

    def stop_usb_sharing(self):
        self.usb_marker = False
        if self.usb_journal is not None:
            forward, interface, _ = self.usb_journal
            if interface in self.usb_rule_instances:
                self.usb_rule_instances.remove(interface)
            if interface not in self.usb_rule_instances:
                self.usb_rules.discard(interface)
            self.forward = forward
            self.usb_journal = None
            self.events.append(('stop_usb', interface))

    def start_phone_station(self, saved, frequency):
        self.station_running = True
        self.events.append(('start_station', frequency))
        if self.station_error:
            raise self.station_error

    def stop_phone_station(self):
        self.station_running = False
        self.addresses.pop(self.interface, None)

    def phone_link(self):
        return self.link

    def phone_station_address(self):
        if self.station_running and self.station_address:
            self.addresses[self.interface] = self.station_address
        return self.station_address

    def uplink_gateway(self):
        return '192.0.2.1'


class UsbPrimaryAPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.saved = {'ssid': 'Phone', 'passphrase': 'synthetic-phone-password'}
        (self.state / 'uplink-hotspot.json').write_text(json.dumps(self.saved), encoding='utf-8')
        (self.state / 'options.json').write_text(json.dumps({
            'car_internet': 'usb_tether',
        }), encoding='utf-8')
        self.p = FakePlatform(self.state)
        self.now = 0
        self.status = {}
        self.address = ipaddress.ip_interface('100.96.0.1/24')
        self.box = {'ssid': 'Box', 'passphrase': 'synthetic-box-password'}

    def boot(self, channel=149, width=40):
        self.u = PhoneUplink(
            self.p, self.state, self.address, self.box, channel, width,
            lambda *args, **kwargs: None, self.p.launch_receiver, self.status,
            say=lambda _: None, clock=lambda: self.now, share=True, primary_ap=True,
        )
        # Match supervisor ownership: marker before the AP, final NAT after receiver.
        self.p.begin_usb_tether(self.address.network)
        self.assertTrue(self.u.start())
        self.p.finish_usb_tether()

    def usb_lost(self):
        self.p.usb_online = self.p.carrier = False

    def due_tick(self):
        self.now = max(self.now, self.u.next_try)
        self.u.tick()

    def found_hotspot(self, frequency=5180):
        self.usb_lost()
        self.p.frequency_found = frequency
        self.due_tick()

    def online_hotspot(self, frequency=5180):
        self.found_hotspot(frequency)
        self.p.link = {'connected': True, 'frequency': frequency}
        self.p.station_address = ipaddress.ip_interface('192.0.2.8/24')
        self.u.tick()
        self.assertEqual(self.u.phase, 'online')

    def assert_lan(self, interface, channel=149, width=40, following=False):
        self.assertTrue(self.u.started)
        self.assertTrue(self.status['tesla_lan_stable'])
        self.assertEqual(self.p.ap_interface, interface)
        self.assertEqual(self.p.services['hostapd'], (
            interface, channel, width, following, self.box['ssid'], self.box['passphrase'],
        ))
        self.assertEqual(self.p.services['receiver'], (interface, channel))
        self.assertEqual(self.p.services['dnsmasq'][:2], (interface, self.address))
        self.assertTrue(self.p.services['dnsmasq'][2]['router'])
        self.assertEqual(self.p.address_interface, interface)
        lan_owners = {name for name, address in self.p.addresses.items()
                      if address.network.overlaps(self.address.network)}
        self.assertEqual(lan_owners, {interface}, 'the old AP still owns the LAN address')
        self.assertEqual(self.p.usb_rules, {interface}, 'stale USB rules survive an AP interface change')
        self.assertEqual(self.p.usb_rule_instances, [interface], 'USB NAT rules were inserted repeatedly')
        self.assertEqual(self.p.phone_rules, set(), 'primary USB compatibility uses official USB NAT only')
        self.assertEqual(self.p.forward, 1)
        self.assertTrue(self.p.usb_marker)

    def assert_primary(self):
        self.assert_lan('wlan0')
        self.assertFalse(self.p.station_running)
        self.assertFalse(self.u.station)
        self.assertIsNone(self.p.phone_journal)
        self.assertEqual(self.p.phone_rules, set())

    def test_usb_boot_keeps_primary_ap_and_saved_radio_despite_hotspot_memory(self):
        phone_uplink.HotspotSearch(self.state, self.saved, lambda: 0).remember(5180)
        self.boot()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'wired')
        self.assertFalse(any(event[0] in ('prepare_phone', 'add_ap', 'scan') for event in self.p.events))

    def test_usb_online_never_scans_or_creates_virtual_ap(self):
        self.boot()
        for _ in range(4):
            self.now += 400
            self.u.tick()
        self.assert_primary()
        self.assertFalse(any(event[0] in ('prepare_phone', 'add_ap', 'scan') for event in self.p.events))
        self.assertEqual(sum(e[0] == 'start_receiver' for e in self.p.events), 1)

    def test_usb_online_on_two_ghz_primary_ap_does_not_periodically_restart(self):
        (self.state / 'uplink-hotspot.json').unlink()
        self.boot(channel=6, width=20)
        for _ in range(4):
            self.now += 400
            self.u.tick()
        self.assert_lan('wlan0', channel=6, width=20, following=False)
        self.assertEqual(sum(e[0] == 'start_receiver' for e in self.p.events), 1)
        self.assertFalse(any(e[0] in ('prepare_phone', 'add_ap', 'scan') for e in self.p.events))

    def test_boot_without_usb_still_starts_primary_ap_before_search(self):
        self.usb_lost()
        phone_uplink.HotspotSearch(self.state, self.saved, lambda: 0).remember(5180)
        self.boot()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'offline')
        self.assertFalse(any(e[0] in ('scan', 'add_ap') for e in self.p.events))

    def test_no_saved_hotspot_does_not_disable_usb_primary_ap(self):
        (self.state / 'uplink-hotspot.json').unlink()
        self.boot()
        self.usb_lost()
        self.now = 500
        self.u.tick()
        self.assert_primary()
        self.assertFalse(any(e[0] in ('scan', 'add_ap') for e in self.p.events))

    def test_search_misses_keep_primary_ap_and_upstream_retry_schedule(self):
        self.boot()
        self.usb_lost()
        self.now = phone_uplink.FAST_WINDOW + 1
        for delay in (20, 40, 60, 60, 60):
            self.u.tick()
            self.assertEqual(self.u.next_try - self.now, delay)
            self.assert_primary()
            self.now = self.u.next_try
        self.assertEqual(sum(e[0] == 'scan' for e in self.p.events), 5)
        self.assertFalse(any(e[0] == 'add_ap' for e in self.p.events))
        self.assertEqual(sum(e[0] == 'start_receiver' for e in self.p.events), 1)

    def test_scan_exception_keeps_primary_ap_and_usb_nat(self):
        self.boot()
        self.usb_lost()
        self.p.scan_error = OSError('synthetic scan failure')
        self.due_tick()
        self.assert_primary()
        self.assertFalse(any(e[0] == 'add_ap' for e in self.p.events))

    def test_found_hotspot_moves_lan_and_usb_nat_to_concurrent_ap(self):
        self.boot()
        self.found_hotspot(5180)
        self.assert_lan('ap0', 36, 20, True)
        self.assertTrue(self.p.station_running)
        self.assertEqual(self.u.phase, 'connecting')

    def test_same_channel_hotspot_still_requires_virtual_ap_and_twenty_mhz(self):
        self.boot()
        self.found_hotspot(5745)
        self.assert_lan('ap0', 149, 20, True)
        self.assertTrue(self.u.station)

    def test_virtual_ap_creation_failure_recovers_primary_ap(self):
        self.boot()
        self.p.ap_add_ok = False
        self.found_hotspot()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'offline')

    def test_station_start_failure_cleans_partial_association_and_recovers_primary_ap(self):
        self.boot()
        self.p.station_error = OSError('synthetic station failure')
        self.found_hotspot()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'offline')

    def test_association_timeout_returns_to_primary_ap_with_saved_width(self):
        self.boot()
        self.found_hotspot()
        self.now += phone_uplink.ASSOCIATION_TIMEOUT + 1
        self.u.tick()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'offline')

    def test_online_link_loss_keeps_grace_then_recovers_primary_ap(self):
        self.boot()
        self.online_hotspot()
        self.p.link = {'connected': False, 'frequency': None}
        self.u.tick()
        self.assertEqual(self.u.phase, 'recovering')
        self.assert_lan('ap0', 36, 20, True)
        self.now += phone_uplink.LINK_LOSS_GRACE + 1
        self.u.tick()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'offline')

    def test_usb_return_from_other_channel_restores_primary_interface_and_full_profile(self):
        self.boot()
        self.online_hotspot(5180)
        self.p.usb_online = self.p.carrier = True
        self.u.tick()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'wired')

    def test_usb_return_from_same_channel_also_restores_primary_interface(self):
        self.boot()
        self.online_hotspot(5745)
        self.p.usb_online = self.p.carrier = True
        self.u.tick()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'wired')



    def test_service_close_after_interface_switch_removes_both_journals_and_restores_forwarding(self):
        self.boot()
        self.online_hotspot()
        # The supervisor stops services before close(), and calls stop_usb_sharing afterwards.
        self.p.services.clear()
        self.u.close()
        self.p.stop_usb_sharing()
        self.assertFalse(self.status['tesla_lan_stable'])
        self.assertEqual(self.p.usb_rules, set())
        self.assertEqual(self.p.phone_rules, set())
        self.assertIsNone(self.p.usb_journal)
        self.assertIsNone(self.p.phone_journal)
        self.assertEqual(self.p.forward, 0)

    def test_usb_return_escalates_failed_station_cleanup(self):
        self.boot()
        self.online_hotspot()
        self.p.usb_online = True
        with patch.object(self.p, 'stop_phone_station', side_effect=OSError('station cleanup failed')):
            with self.assertRaises(phone_uplink.LanRecoveryFailed):
                self.u.tick()
        self.assertFalse(self.u.started)

    def test_association_timeout_escalates_failed_station_cleanup(self):
        self.boot()
        self.found_hotspot()
        self.now += phone_uplink.ASSOCIATION_TIMEOUT + 1
        with patch.object(self.p, 'stop_phone_station', side_effect=OSError('station cleanup failed')):
            with self.assertRaises(phone_uplink.LanRecoveryFailed):
                self.u.tick()
        self.assertFalse(self.u.started)


    def fail_after_call(self, target, name, once=True):
        """Fail after a fake mutation, so cleanup must undo partial ownership."""
        original = getattr(target, name)
        calls = []

        def failure(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(1)
            if not once or len(calls) == 1:
                raise OSError('synthetic partial ' + name + ' failure')
            return result

        return patch.object(target, name, side_effect=failure)

    def assert_partial_transition_recovers(self, name):
        self.boot()
        target = self.u if name == 'start_receiver' else self.p
        with self.fail_after_call(target, name):
            self.found_hotspot()
        self.assert_primary()
        self.assertEqual(self.u.phase, 'offline')
        self.assertFalse(any(event[0] == 'start_station' for event in self.p.events))
        # A subsequent attempt still works without stale virtual-interface or NAT ownership.
        self.online_hotspot(5745)
        self.assert_lan('ap0', 149, 20, True)
        self.p.usb_online = self.p.carrier = True
        self.u.tick()
        self.assert_primary()

    def test_partial_prepare_phone_failure_returns_to_primary_ap(self):
        self.assert_partial_transition_recovers('prepare_phone_uplink')

    def test_partial_virtual_ap_creation_failure_returns_to_primary_ap(self):
        self.assert_partial_transition_recovers('add_phone_ap')

    def test_partial_hotspot_configuration_failure_returns_to_primary_ap(self):
        self.assert_partial_transition_recovers('configure_hotspot')

    def test_partial_dhcp_start_failure_returns_to_primary_ap(self):
        self.assert_partial_transition_recovers('start_dhcp')

    def test_partial_usb_nat_install_failure_returns_to_primary_ap_without_duplicate_rules(self):
        self.assert_partial_transition_recovers('finish_usb_tether')

    def test_partial_receiver_start_failure_returns_to_primary_ap(self):
        self.assert_partial_transition_recovers('start_receiver')

    def test_partial_lan_health_check_failure_returns_to_primary_ap(self):
        self.assert_partial_transition_recovers('check_lan_children')

    def test_failed_primary_recovery_raises_and_does_not_advertise_a_working_lan(self):
        for name in ('configure_hotspot', 'start_dhcp', 'finish_usb_tether',
                     'start_receiver', 'check_lan_children'):
            with self.subTest(stage=name):
                self.setUp()
                self.boot()
                target = self.u if name == 'start_receiver' else self.p
                # Concurrent setup and primary recovery fail at the same boundary.
                with self.fail_after_call(target, name, once=False):
                    with self.assertRaises(phone_uplink.LanRecoveryFailed):
                        self.found_hotspot()
                self.assertFalse(self.u.started)
                self.assertFalse(self.status['tesla_lan_stable'])
                self.p.services.clear()
                self.u.close()
                self.p.stop_usb_sharing()
                self.assertEqual(self.p.usb_rule_instances, [])
                self.assertEqual(self.p.usb_rules, set())
                self.assertEqual(self.p.phone_rules, set())
                self.assertIsNone(self.p.phone_journal)
                self.assertIsNone(self.p.usb_journal)
                self.assertEqual(self.p.forward, 0)

    def test_repeated_interface_transitions_keep_one_nat_rule_set_and_restore_forwarding_on_close(self):
        self.boot()
        for frequency, channel in ((5180, 36), (5745, 149), (2437, 6), (5180, 36)):
            with self.subTest(frequency=frequency):
                self.online_hotspot(frequency)
                self.assert_lan('ap0', channel, 20, True)
                self.p.usb_online = self.p.carrier = True
                self.u.tick()
                self.assert_primary()
        self.p.services.clear()
        self.u.close()
        self.p.stop_usb_sharing()
        self.assertEqual(self.p.forward, 0)
        self.assertEqual(self.p.usb_rule_instances, [])
        self.assertEqual(self.p.usb_rules, set())
        self.assertEqual(self.p.phone_rules, set())
        self.assertIsNone(self.p.phone_journal)
        self.assertIsNone(self.p.usb_journal)



if __name__ == '__main__':
    unittest.main()
