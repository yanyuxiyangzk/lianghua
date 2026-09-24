import json
import tempfile
import unittest
from pathlib import Path

from workflow_status import read_live_status, read_mining_progress


class WorkflowStatusTests(unittest.TestCase):
    def test_running_batch_on_late_page_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'scheduler_live.json'
            p.write_text(json.dumps({'ts': 990, 'running': {'multitype_mine': 800},
                                     'next': {'multitype_mine': '09-24 19:30'}}))
            result = read_live_status(tmp, now=1000)
            self.assertTrue(result['fresh'])
            self.assertEqual(result['running'], {'multitype_mine': 800})
            self.assertEqual(result['next_mining'], '09-24 19:30')
            # A subsequent heartbeat clears the finished batch without SSE replay.
            p.write_text(json.dumps({'ts': 1000, 'running': {}}))
            self.assertEqual(read_live_status(tmp, now=1001)['running'], {})

    def test_stale_or_invalid_snapshot_never_claims_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'scheduler_live.json'
            cases = ['', '{', 'null', '{}',
                     json.dumps({'ts': 820, 'running': {'multitype_mine': 800}}),
                     json.dumps({'ts': 1001, 'running': {'multitype_mine': 800}}),
                     json.dumps({'ts': float('nan'), 'running': {}})]
            self.assertFalse(read_live_status(tmp, now=1000)['fresh'])
            for content in cases:
                p.write_text(content)
                result = read_live_status(tmp, now=1000)
                self.assertFalse(result['fresh'], content)
                self.assertEqual(result['running'], {})

    def test_type_rotation_survives_reload_and_rejects_old_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mining_progress.json'
            live = {'fresh': True, 'running': {'multitype_mine': 800}}
            p = dict(started_at=801, updated_at=900, status='running',
                     factor_type='量价', type_index=1, total_types=10,
                     rotation=2, rotations=2)
            path.write_text(json.dumps(p))
            self.assertEqual(read_mining_progress(tmp, live, 1000)['factor_type'], '量价')
            p.update(factor_type='资金流', type_index=2, rotation=1)
            path.write_text(json.dumps(p))
            self.assertEqual(read_mining_progress(tmp, live, 1000)['factor_type'], '资金流')
            live['running']['multitype_mine'] = 950
            self.assertIsNone(read_mining_progress(tmp, live, 1000))
            live['fresh'] = False
            self.assertIsNone(read_mining_progress(tmp, live, 1000))

    def test_invalid_start_times_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'scheduler_live.json').write_text(json.dumps({
                'ts': 990, 'running': {'multitype_mine': 'oops', 'loopengine': -1}}))
            self.assertEqual(read_live_status(tmp, now=1000)['running'], {})


if __name__ == '__main__':
    unittest.main()
