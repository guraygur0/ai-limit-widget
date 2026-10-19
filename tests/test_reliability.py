import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usage
from desktop_layer import DesktopLayer

class AuthBoundaryTests(unittest.TestCase):
    def test_expired_claude_login_makes_no_network_request(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'auth.json'
            p.write_text(json.dumps({'claudeAiOauth': {'expiresAt': 1, 'accessToken': 'test-only'}}))
            with patch.object(usage, 'CLAUDE_CREDS', p), patch.object(usage.urllib.request, 'build_opener') as network:
                result = usage._fetch('claude')
            network.assert_not_called()
            self.assertEqual(result.get('http_status'), 401)
            self.assertNotIn('test-only', json.dumps(result))

    def test_malformed_codex_credentials_do_not_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'auth.json'
            for tokens in ([1], 'broken', 7):
                p.write_text(json.dumps({'tokens': tokens}))
                with patch.object(usage, 'CODEX_AUTH', p), patch.object(usage.urllib.request, 'build_opener') as network:
                    result = usage._fetch('codex')
                self.assertFalse(result['ok'])
                network.assert_not_called()

class DesktopBoundaryTests(unittest.TestCase):
    def make_layer(self, visible):
        class Fake:
            def FindWindowExW(self, parent, after, kind, title):
                return {None: 1, 1: 2, 2: 3, 3: 0}[after]
            def IsWindowVisible(self, hwnd):
                return hwnd in visible
            def GetWindowRect(self, hwnd, ptr):
                ptr._obj.right = 198 if hwnd == 1 else 2560
                ptr._obj.bottom = 56 if hwnd == 1 else 1600
                return 1
            def FindWindowW(self, *args):
                return 9
        layer = DesktopLayer.__new__(DesktopLayer)
        layer.u = Fake()
        return layer

    def test_visible_wallpaper_above_progman_is_selected(self):
        self.assertEqual(self.make_layer({1, 2, 3}).find_desktop(), 2)

    def test_hidden_or_tiny_workers_do_not_hide_widgets(self):
        self.assertEqual(self.make_layer({1}).find_desktop(), 9)

if __name__ == '__main__':
    unittest.main()
