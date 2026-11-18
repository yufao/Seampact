"""Private sampling receipts stay numeric and do not change usage accounting."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.api_accounting import private_receipt, summarize


class PrivateSamplingReceipts(unittest.TestCase):
    def test_refchecker_and_nemo_aliases_keep_effective_value_and_fixed_sources(self):
        fixtures = [({'temperature': 1.0, 'temperature_source': 'service.parameters.temperature'}, 1.0, 'connection_explicit'),
                    ({'temperature': 1e-5, 'temperature_source': 'upstream_triplet_default'}, 1e-5, 'upstream_triplet_default'),
                    ({'effective_temperature': .001, 'temperature_source': 'upstream_rail_default'}, .001, 'upstream_rail_default'),
                    ({'effective_temperature': 0, 'temperature_source': 'service.parameters.extra_body.temperature'}, 0, 'connection_explicit')]
        for fixture, expected, source in fixtures:
            with self.subTest(fixture=fixture):
                value = private_receipt(dict(fixture, usage_received=True, input_tokens=3, output_tokens=2,
                                             prompt='PRIVATE_PROMPT', api_key='PRIVATE_CREDENTIAL'))
                self.assertEqual(value['effective_temperature'], expected)
                self.assertEqual(value['temperature_source'], source)
                self.assertNotIn('PRIVATE_', json.dumps(value))

    def test_invalid_values_and_freeform_sources_cannot_leak(self):
        for invalid in (True, False, -1, 2.1, float('inf'), float('nan'), 'PRIVATE', {}, None):
            with self.subTest(value=invalid):
                value = private_receipt(dict(effective_temperature=invalid, temperature_source='PRIVATE'))
                self.assertNotIn('effective_temperature', value)
                self.assertNotIn('temperature_source', value)
        value = private_receipt(dict(effective_temperature=1, temperature_source='PRIVATE_MODEL_OR_KEY'))
        self.assertEqual(value['temperature_source'], 'unknown')
        self.assertNotIn('PRIVATE_', json.dumps(value))

    def test_sampling_fields_do_not_create_requests_or_tokens(self):
        raw = [dict(usage_received=True, total_tokens=5, temperature=1,
                    temperature_source='service.parameters.temperature'),
               dict(usage_returned=True, input_tokens=4, output_tokens=2, effective_temperature=.001,
                    temperature_source='upstream_rail_default')]
        direct = summarize([], [dict(model_usage=[dict(data=row) for row in raw])])
        filtered = summarize([], [dict(model_usage=[dict(data=private_receipt(row)) for row in raw])])
        self.assertEqual(filtered, direct)
        self.assertEqual((filtered['tokens'], filtered['capability_requests'], filtered['unknown_requests']), (11, 2, 0))
        unknown = private_receipt(dict(temperature=1, temperature_source='service.parameters.temperature', usage_received=False))
        self.assertFalse(unknown['usage_returned'])


if __name__ == '__main__':
    unittest.main()
