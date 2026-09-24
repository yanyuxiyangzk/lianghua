import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import common
import scheduler
import validation_worker as worker
from validation_trace import ValidationTimeout


class WorkerTests(unittest.TestCase):
    def test_timeout_kills_group_and_never_returns_partial_result(self):
        proc = MagicMock(pid=12345)
        proc.wait.side_effect = [subprocess.TimeoutExpired('worker', .1), -9]
        with patch.object(worker.subprocess, 'Popen', return_value=proc), patch.object(worker.os, 'killpg') as kill:
            with self.assertRaises(ValidationTimeout):
                worker.compute_isolated('s', {}, '2026-09-24', .1)
        kill.assert_called_once_with(12345, worker.signal.SIGKILL)

    def test_worker_output_is_returned_without_publishing(self):
        def launch(args, **kwargs):
            Path(args[-1]).write_text(json.dumps({'ok': True, 'status': 'degraded'}))
            return MagicMock(pid=12345, returncode=0)
        with patch.object(worker.subprocess, 'Popen', side_effect=launch), patch.object(worker.os, 'killpg'):
            result = worker.compute_isolated('s', {}, '2026-09-24', 1)
        self.assertTrue(result['ok'])
        self.assertNotIn('report_id', result)

    def test_duplicate_lock_skips_computation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(common, 'DATA_DIR', Path(tmp)), \
             patch.object(scheduler, 'revalidate_strategy') as compute:
            locks = Path(tmp) / 'validation_locks'
            locks.mkdir()
            with (locks / (hashlib.sha256(b's').hexdigest()+'.lock')).open('a') as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(worker.run_bounded('s')['assessment_status'], 'busy')
            worker.run_bounded('s', 2)
            compute.assert_called_once_with('s', timeout_seconds=2, isolated=True)

    def test_scheduler_honors_pool_and_stopped_status(self):
        packs = {'a': {'pool_name': 'p', 'status': 'active'},
                 'b': {'pool_name': 'q', 'status': 'active'},
                 'c': {'pool_name': 'p', 'status': 'archived'}}
        import library
        with patch.object(library, 'list_strategies', return_value=packs), \
             patch.object(worker, 'run_bounded', return_value={'ok': False, 'error': 'timeout'}) as run:
            scheduler.job_strategy_revalidate('p')
        run.assert_called_once_with('a', timeout_seconds=180)
