"""USB routing must not turn real websites into the local settings server."""
import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import supervisor
from phone_uplink import PhoneUplink


class UsbPublicDnsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        (self.state / 'options.json').write_text(json.dumps({
            'car_internet': 'usb_tether', 'tesla': True,
        }), encoding='utf-8')

    def controller(self, share):
        return PhoneUplink(Mock(), self.state, ipaddress.ip_interface('100.96.0.1/24'),
                           {}, 149, 40, Mock(), Mock(),
                           {'tesla': True, 'own_name': 'viewer.example.test'}, share=share)

    def test_usb_normal_ap_only_overrides_viewer_name(self):
        self.assertEqual(supervisor.hotspot_names(self.state, 'viewer.example.test', 'box_hotspot'),
                         ['viewer.example.test'])

    def test_usb_persistent_ap_only_overrides_viewer_name(self):
        self.assertEqual(self.controller(True).local_names(), ['viewer.example.test'])

    def test_non_usb_phone_policy_is_unchanged(self):
        self.assertIn('www.baidu.com', self.controller(False).local_names())

    def test_non_usb_normal_ap_policy_is_unchanged(self):
        (self.state / 'options.json').write_text(json.dumps({
            'car_internet': 'none', 'tesla': True,
        }), encoding='utf-8')
        self.assertIn('www.baidu.com', supervisor.hotspot_names(self.state, None, 'box_hotspot'))

    def test_primary_policy_is_limited_to_n1_broadcom(self):
        platform = Mock(interface='wlan0')
        for is_n1, driver, expected in ((True, 'brcmfmac', True),
                                        (True, 'rtw88_8822bu', False),
                                        (False, 'brcmfmac', False)):
            with self.subTest(is_n1=is_n1, driver=driver):
                with patch.object(supervisor.board_phicomm_n1, 'is_n1', return_value=is_n1), \
                        patch.object(supervisor, 'Path') as path:
                    path.return_value.__truediv__.return_value.__truediv__.return_value.resolve.return_value.name = driver
                    self.assertIs(supervisor.primary_usb_ap(platform), expected)


if __name__ == '__main__':
    unittest.main()
