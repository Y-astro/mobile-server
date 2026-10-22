from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from telemetry import Telemetry


class TelemetryTests(unittest.TestCase):
    def test_unknown_metrics_are_null_not_plausible_defaults(self):
        sample = Telemetry.empty()
        self.assertIsNone(sample['network']['wifi_speed'])
        self.assertIsNone(sample['network']['rssi'])
        self.assertIsNone(sample['network']['wan_ping'])
        self.assertIsNone(sample['battery']['percentage'])
        self.assertIsNone(sample['cpu_temp'])
        self.assertEqual(sample['load_avg'], [None]*3)

    def test_snapshot_does_not_mutate_cache_and_reports_stale(self):
        sampler = Telemetry()
        with patch.object(sampler, 'start'):
            result = sampler.snapshot()
            self.assertTrue(result['stale'])
            result['network']['wifi_speed'] = 999
            self.assertIsNone(sampler.snapshot()['network']['wifi_speed'])

    def test_missing_android_loadavg_does_not_discard_other_readings(self):
        import telemetry
        with tempfile.TemporaryDirectory() as home:
            # Android's Python omits this optional os API entirely.
            with patch.object(telemetry.os, 'getloadavg', create=True):
                del telemetry.os.getloadavg
                with patch('telemetry.bounded_command', side_effect=OSError('Unavailable API')):
                    sample = Telemetry(home).collect()
            self.assertEqual(sample['load_avg'], [None]*3)
            self.assertIsNotNone(sample['sampled_at'])
            self.assertIsNotNone(sample['storage']['total_gb'])
            self.assertIsNotNone(sample['ram']['total_mb'])
            self.assertNotIn('load_avg', sample['sources'])

if __name__ == '__main__': unittest.main()
