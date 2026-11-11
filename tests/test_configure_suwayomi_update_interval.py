import unittest
from unittest.mock import patch, MagicMock
import io
import json
from tools.configure_suwayomi_update_interval import (
    get_current_settings,
    set_global_update_interval,
    main,
)

class TestConfigureSuwayomiUpdateInterval(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_get_current_settings_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": {
                "settings": {
                    "globalUpdateInterval": 12.0,
                    "updateMangas": True
                }
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = get_current_settings("http://test/graphql")
        self.assertEqual(res["globalUpdateInterval"], 12.0)
        self.assertTrue(res["updateMangas"])

    @patch("urllib.request.urlopen")
    def test_get_current_settings_graphql_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "errors": [{"message": "Something went wrong"}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with self.assertRaises(RuntimeError) as cm:
            get_current_settings("http://test/graphql")
        self.assertIn("GraphQL error fetching settings", str(cm.exception))

    @patch("urllib.request.urlopen")
    def test_set_global_update_interval_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": {
                "setSettings": {
                    "settings": {
                        "globalUpdateInterval": 0.0,
                        "updateMangas": True
                    }
                }
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = set_global_update_interval(0.0, "http://test/graphql")
        self.assertEqual(res["globalUpdateInterval"], 0.0)
        self.assertTrue(res["updateMangas"])

        # Validate request payload
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["variables"]["input"]["settings"]["globalUpdateInterval"], 0.0)

    @patch("urllib.request.urlopen")
    def test_set_global_update_interval_graphql_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "errors": [{"message": "Validation failed"}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with self.assertRaises(RuntimeError) as cm:
            set_global_update_interval(0.0, "http://test/graphql")
        self.assertIn("GraphQL error setting globalUpdateInterval", str(cm.exception))

    @patch("urllib.request.urlopen")
    @patch("sys.argv", ["configure_suwayomi_update_interval.py", "--dry-run"])
    def test_main_dry_run(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": {
                "settings": {
                    "globalUpdateInterval": 12.0,
                    "updateMangas": True
                }
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            code = main()
            self.assertEqual(code, 0)
            self.assertIn("Dry-run mode", mock_out.getvalue())
            self.assertEqual(mock_urlopen.call_count, 1)

if __name__ == "__main__":
    unittest.main()
