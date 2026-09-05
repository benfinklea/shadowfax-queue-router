"""CT boundaries, job identity and zero-network accounting for Fleet tiles."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import queue_router as q


class LocalJobTotalsTest(unittest.TestCase):
    def test_ct_boundary_dedup_attempts_and_cached_active_jobs(self):
        now = q.datetime.fromisoformat('2026-09-01T06:00:00+00:00')
        def row(run, end, duration=60, runner='gandalf-1', attempt=1):
            return dict(run_id=run, run_attempt=attempt, job_name='test',
                        runner_name=runner, ts_completed=end, duration_s=duration)
        rows = [row(1, '2026-09-01T05:00:30Z'),  # starts before CT midnight
                row(2, '2026-09-01T05:02:00Z'),
                row(2, '2026-09-01T05:02:00Z'),  # duplicate collector row
                row(2, '2026-09-01T05:04:00Z', attempt=2),
                row(3, '2026-09-01T05:05:00Z', runner='runs-on--cloud'),
                row(4, '2026-09-01T05:05:00Z', runner='GitHub Actions 1'),
                row(5, '2026-09-01T04:59:59Z')]
        jobs = [dict(name='test', run_attempt=1, runner_name='gandalf-1', labels=['self-hosted'],
                     started_at='2026-09-01T05:01:00Z', completed_at='2026-09-01T05:02:00Z')]
        active = [dict(name='active', runner_name='custom-worker', labels=['self-hosted'],
                       started_at='2026-09-01T05:59:00Z', completed_at=None)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'jobs-2026-09.jsonl').write_text('\n'.join(map(json.dumps, rows)) + '\n{"partial":')
            cache = {('jobs', 2): (0, Mock(json=lambda: {'jobs': jobs})),
                     ('jobs', 6): (0, Mock(json=lambda: {'jobs': active}))}
            with patch.object(q, 'CI_TIMING_STATE', root), patch.object(q, 'runner_reads', cache), patch.object(q.requests, 'get') as get:
                result = q._local_jobs_today([], now)
                self.assertEqual((result['jobs_today'], result['jobs_done']), (3, 3))
                self.assertEqual(len(result['jobs_smoothed']), 61)
                self.assertEqual(result['jobs_smoothed'][-1]['ts'], now.isoformat())
                self.assertAlmostEqual(result['jobs_smoothed'][-1]['n'], 2 / 3)
                get.assert_not_called()

    def test_missing_records_are_unknown(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(q, 'CI_TIMING_STATE', Path(directory)), patch.object(q.requests, 'get') as get:
            result = q._local_jobs_today([])
            self.assertIsNone(result['jobs_today'])
            self.assertIsNone(result['jobs_done'])
            self.assertEqual(result['jobs_smoothed'], [])
            get.assert_not_called()
