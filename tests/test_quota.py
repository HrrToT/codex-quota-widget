import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'quota_widget.py'
spec = importlib.util.spec_from_file_location('widget', path)
widget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(widget)

class QuotaTests(unittest.TestCase):
    def test_multibucket_precedence(self):
        data = {'rateLimits': {'primary': {'usedPercent': 99}}, 'rateLimitsByLimitId': {
            'codex': {'primary': {'usedPercent': 44, 'windowDurationMins': 10080}, 'secondary': None},
            'other': {'primary': {'usedPercent': 25, 'windowDurationMins': 300}}}}
        rows = widget.quota_rows(data)
        self.assertEqual([r['remaining'] for r in rows], [56, 75])
        self.assertEqual([r['period'] for r in rows], ['7 天', '5 小时'])

    def test_legacy_and_secondary(self):
        rows = widget.quota_rows({'rateLimits': {'primary': {'usedPercent': 10}, 'secondary': {'usedPercent': 20}}})
        self.assertEqual([r['remaining'] for r in rows], [90, 80])

    def test_missing_not_zero(self):
        self.assertEqual(widget.quota_rows({'rateLimits': None}), [])
        rows = widget.quota_rows({'rateLimits': {'primary': {'usedPercent': None}}})
        self.assertIsNone(rows[0]['remaining'])
        self.assertEqual(widget.reset_text(None), '重置时间未提供')

    def test_bounds_and_nonfinite(self):
        for value, expected in [(120, 0), (-2, 100), (float('nan'), None), ('50', None)]:
            row = widget.quota_rows({'rateLimits': {'primary': {'usedPercent': value}}})[0]
            self.assertEqual(row['remaining'], expected)

if __name__ == '__main__':
    unittest.main()
