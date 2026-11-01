import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import hotspot_radio as radio

PHY = '''Wiphy phy0
 Band 1:
  HT20/HT40
  Frequencies:
   * 2412 MHz [1] (20.0 dBm)
   * 2437 MHz [6] (20.0 dBm)
   * 2462 MHz [11] (20.0 dBm)
   * 2467 MHz [12] (disabled)
 Band 2:
  HT20/HT40
  VHT Capabilities (0x00001020):
   Supported Channel Width: neither 160 nor 80+80
  Frequencies:
   * 5180 MHz [36] (20.0 dBm)
   * 5200 MHz [40] (20.0 dBm)
   * 5220 MHz [44] (20.0 dBm)
   * 5240 MHz [48] (20.0 dBm)
   * 5260 MHz [52] (20.0 dBm) (radar detection)
   * 5280 MHz [56] (20.0 dBm) (no IR)
   * 5745 MHz [149] (20.0 dBm)
   * 5765 MHz [153] (20.0 dBm)
   * 5785 MHz [157] (20.0 dBm)
   * 5805 MHz [161] (20.0 dBm)
   * 5825 MHz [165] (20.0 dBm)
'''
ACTUAL = '''Interface wlan0
 type AP
 channel 149 (5745 MHz), width: 80 MHz, center1: 5775 MHz
 txpower 31.00 dBm
'''
BASE = {'band': '5', 'channel': 149, 'width': 80}


class CapabilitiesTests(unittest.TestCase):
    def test_current_iw_decimal_frequency_format(self):
        import re
        decimal = re.sub(r'(\d+) MHz', r'\1.0 MHz', PHY)
        self.assertEqual(radio.capabilities(decimal), radio.capabilities(PHY))

    def test_filter_channels_and_all_subchannels(self):
        caps = radio.capabilities(PHY)
        self.assertEqual(caps['2.4'], {1: [20], 6: [20], 11: [20]})
        self.assertEqual(caps['5'][149], [20, 40, 80])
        self.assertEqual(caps['5'][165], [20])
        self.assertNotIn(52, caps['5'])
        self.assertNotIn(56, caps['5'])
        caps = radio.capabilities(PHY.replace('[161] (20.0 dBm)', '[161] (disabled)'))
        self.assertEqual(caps['5'][149], [20, 40])
        self.assertEqual(caps['5'][157], [20])

    def test_only_widths_supported_by_hostapd_generator(self):
        self.assertNotIn(80, radio.capabilities(PHY.replace('VHT Capabilities', 'Other'))['5'][36])
        self.assertEqual(radio.capabilities(''), {})

    def test_validate_band_channel_width_together(self):
        caps = radio.capabilities(PHY)
        self.assertEqual(radio.validate({'band': '5', 'channel': '149', 'width': '80'}, caps), BASE)
        for band, channel, width in [('2.4', '149', '20'), ('5', '165', '80'),
                                     ('2.4', '6', '40'), ('5', '52', '20'), ('5', '149', '160'),
                                     ('5', '149;reboot', '20')]:
            with self.subTest(band=band, channel=channel, width=width), self.assertRaises(ValueError):
                radio.validate(dict(band=band, channel=channel, width=width), caps)

    def test_actual_never_uses_requested_settings_as_measurement(self):
        self.assertEqual(radio.actual(ACTUAL), dict(BASE, frequency=5745))
        self.assertIsNone(radio.actual(ACTUAL.replace('type AP', 'type managed')))
        self.assertIsNone(radio.actual('Interface wlan0\n type AP\n'))


class TransactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.now = 100
        self.armed = []
        if os.name == 'nt':
            mock = patch('hotspot_radio.fcntl.flock')
            mock.start()
            self.addCleanup(mock.stop)
        self.store = radio.Store(self.state, BASE, 'boot-a', lambda: self.now,
                                 self.armed.append, lambda _: True)
        self.candidate = dict(BASE, width=40)

    def test_guard_armed_before_candidate_persists(self):
        def arm(token):
            self.assertFalse(self.store.path.exists())
            self.armed.append(token)
        self.store.arm = arm
        token = self.store.begin(self.candidate)
        self.assertEqual(self.armed, [token])
        self.assertEqual(self.store.effective(), self.candidate)
        # Independent process instances see the same transaction, not an in-memory timer.
        other = radio.Store(self.state, BASE, 'boot-a', lambda: self.now, arm, lambda _: True)
        self.assertEqual(other.snapshot()['pending']['id'], token)

    def test_failed_guard_and_noop_never_apply(self):
        self.assertIsNone(self.store.begin(BASE))
        self.assertEqual(self.armed, [])
        def fail(_):
            raise RuntimeError('guard failed')
        self.store.arm = fail
        with self.assertRaises(ValueError):
            self.store.begin(self.candidate)
        self.assertEqual(self.store.effective(), BASE)

    def test_only_matching_actual_can_confirm_and_stale_token_rejected(self):
        token = self.store.begin(self.candidate)
        for key, measured in [('wrong', self.candidate), (token, None), (token, BASE)]:
            with self.assertRaises(ValueError):
                self.store.confirm(key, measured)
        self.store.confirm(token, dict(self.candidate, frequency=5745))
        self.now += 500
        self.assertFalse(self.store.expire(token))
        self.assertEqual(self.store.effective(), self.candidate)

    def test_timeout_restores_only_radio_and_rejects_late_confirmation(self):
        untouched = self.state / 'options.json'
        untouched.write_text('{"car_internet":"usb_tether","uplink_retry_max":100}', encoding='utf-8')
        before = untouched.read_bytes()
        token = self.store.begin(self.candidate)
        self.now += radio.CONFIRM_SECONDS
        with self.assertRaises(ValueError):
            self.store.confirm(token, self.candidate)
        self.assertTrue(self.store.expire(token))
        self.assertEqual(self.store.effective(), BASE)
        self.assertEqual(untouched.read_bytes(), before)
        self.assertFalse(self.store.expire(token))

    def test_reboot_or_missing_guard_recovers_before_radio_is_used(self):
        self.store.begin(self.candidate)
        self.assertFalse(self.store.recover())
        self.store.boot = 'boot-b'
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.effective(), BASE)
        self.store.begin(self.candidate)
        self.store.alive = lambda _: False
        self.assertTrue(self.store.recover())
        self.assertEqual(self.store.effective(), BASE)

    def test_second_change_blocked_and_old_guard_cannot_revert_new_trial(self):
        first = self.store.begin(self.candidate)
        with self.assertRaises(ValueError):
            self.store.begin(dict(BASE, channel=36))
        self.store.cancel(first)
        self.store.acknowledge_restart(first)
        second = self.store.begin(dict(BASE, channel=36))
        self.now += radio.CONFIRM_SECONDS
        self.assertFalse(self.store.expire(first))
        self.assertEqual(self.store.snapshot()['pending']['id'], second)
        self.assertTrue(self.store.expire(second))


class GuardTests(unittest.TestCase):
    setUp = TransactionTests.setUp
    def test_guard_retries_failed_restart_and_keeps_recovery_record(self):
        token = self.store.begin(self.candidate)
        self.now += radio.CONFIRM_SECONDS
        calls = []
        def restart():
            calls.append('restart')
            if len(calls) == 1:
                raise OSError('temporary failure')
        def sleep(seconds):
            self.now += seconds
        radio.guard(self.store, token, restart, sleep)
        self.assertEqual(calls, ['restart', 'restart'])
        self.assertEqual(self.store.effective(), BASE)
        self.assertNotIn('restart', self.store.snapshot())

    def test_confirmed_trial_does_not_restart(self):
        token = self.store.begin(self.candidate)
        self.store.confirm(token, self.candidate)
        def fail():
            self.fail('Confirmed settings must not be reverted')
        radio.guard(self.store, token, fail, lambda _: None)

    def test_cancel_guard_survives_process_restart(self):
        token = self.store.begin(self.candidate)
        self.store.cancel(token)
        self.now += 2
        other = radio.Store(self.state, BASE, 'boot-a', lambda: self.now,
                            self.armed.append, lambda _: True)
        calls = []
        radio.guard(other, token, lambda: calls.append('restart'), lambda _: None)
        self.assertEqual(calls, ['restart'])


class PageTests(unittest.TestCase):
    setUp = TransactionTests.setUp

    def page(self):
        from settings_page import SettingsPage
        import setup_wizard
        (self.state / 'setup.json').write_text(json.dumps({'done': True, 'mode': 'box_hotspot',
                                                        'iphone': 'wired'}), encoding='utf-8')
        (self.state / 'options.json').write_text('{"car_internet":"usb_tether"}', encoding='utf-8')
        control = radio.Radio(self.store, lambda: 'wlan0')
        control.inspect = lambda: dict(self.store.snapshot(), capabilities=radio.capabilities(PHY),
                                       actual=BASE, remaining=180)
        page = SettingsPage(self.state, lambda: {'mode': 'ap'}, lambda _: None, lambda: [], radio=control)
        page.apply_later = self.armed.append
        return page

    def test_band_controls_and_actual_state_share_hotspot_page(self):
        page = self.page()
        html = page.render_hotspot_change('')
        for text in ('name=band', 'name=channel', 'name=width', '实际运行', '发射功率', '自动', '149', '80 MHz'):
            self.assertIn(text, html)
        self.assertNotIn('name=txpower', html)

    def test_csrf_and_upstream_mode_block_changes(self):
        page = self.page()
        form = dict(self.candidate, nonce='wrong')
        with self.assertRaises(ValueError):
            page.apply('/hotspot-radio', form)
        form['nonce'] = page.pairing.nonce
        (self.state / 'options.json').write_text('{"car_internet":"phone_hotspot"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            page.apply('/hotspot-radio', form)
        self.assertFalse(self.store.path.exists())
        self.assertIn('跟随手机热点', page.render_hotspot_change(''))

    def test_trial_blocks_other_network_writes_and_returns_confirmation(self):
        page = self.page()
        page.apply('/hotspot-radio', dict(self.candidate, nonce=page.pairing.nonce))
        self.assertIn('hotspot radio changed', self.armed)
        for path in ('/hotspot', '/internet', '/setup', '/update'):
            with self.assertRaises(ValueError):
                page.apply(path, {'nonce': page.pairing.nonce})
        html = page.render_hotspot_change('')
        self.assertIn('确认保留', html)
        self.assertIn('恢复原设置', html)
        self.assertIn('180', html)


@unittest.skipUnless(os.name == 'posix', 'Requires Linux flock and fork')
class LinuxTransactionTests(unittest.TestCase):
    setUp = TransactionTests.setUp

    def test_two_processes_cannot_start_overlapping_trials(self):
        import multiprocessing
        ctx = multiprocessing.get_context('fork')
        ready = ctx.Barrier(2)
        results = ctx.Queue()
        def begin():
            ready.wait(timeout=5)
            try:
                self.store.begin(self.candidate)
                results.put('started')
            except ValueError:
                results.put('blocked')
        children = [ctx.Process(target=begin) for _ in range(2)]
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(sorted(results.get(timeout=2) for _ in children), ['blocked', 'started'])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.with_suffix('.lock').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
