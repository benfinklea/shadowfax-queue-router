"""Token-budget and runner attribution contracts for the running-jobs dropdown."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import requests
import queue_router as q


class RunnerJobsTest(unittest.TestCase):
    def setUp(self):
        q.runner_reads.clear()
        q.runner_jobs_cache.update(data=None, ts=0)

    def response(self, payload, links=None):
        return Mock(json=lambda: payload, links=links or {})

    def test_join_cache_and_concurrent_readers(self):
        inventory = [{'name': name, 'busy': True, 'labels': []} for name in
                     ['gandalf-1', 'frodo-2', 'runs-on--abc']]
        run = dict(id=42, name='CI', head_branch='fix-it', pull_requests=[{'number': 99}], html_url='https://github.com/armbrain-io/armbrain/actions/runs/42')
        job = dict(status='in_progress', runner_name='gandalf-1', name='Build', started_at='2026-09-05T00:00:00Z')
        pages = [self.response({'resources': {'core': {'remaining': 1500}}}),
                 self.response({'runners': inventory}), self.response({'workflow_runs': [run]}),
                 self.response({'jobs': [job]})]
        with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=pages) as get:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: q.get_runner_jobs(), range(4)))
            self.assertEqual(get.call_count, 4)
            self.assertEqual(sum(r['github_calls'] for r in results), 4)
            rows = {j['runner']: j for j in results[0]['jobs']}
            self.assertEqual(rows['gandalf-1']['pr'], 99)
            self.assertEqual(rows['frodo-2']['job'], 'busy - job not visible')
            self.assertEqual(rows['runs-on--abc']['pool'], 'aws')
            q._runner_run_jobs(42, {})
            self.assertEqual(get.call_count, 4)
            self.assertTrue(q.app.test_client().get('/api/runner_jobs?fresh=1').json['cached'])

    def test_low_budget_retains_snapshot(self):
        q.runner_jobs_cache.update(data=dict(jobs=[{'runner': 'frodo-1'}], available=True), ts=0)
        with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', return_value=self.response({'resources': {'core': {'remaining': 999}}})) as get:
            result = q.get_runner_jobs()
            self.assertTrue(result['stale'])
            self.assertEqual(result['jobs'], [{'runner': 'frodo-1'}])
            self.assertEqual(get.call_count, 1)
            self.assertTrue(q.get_runner_jobs()['cached'])

    def test_failed_job_read_is_not_retried_in_window(self):
        with patch.object(q, '_runner_budget', return_value=1500), patch.object(q.requests, 'get', side_effect=requests.Timeout('timeout')) as get:
            for _ in range(2):
                with self.assertRaises(requests.Timeout):
                    q._runner_run_jobs(42, {})
            self.assertEqual(get.call_count, 1)

    def test_job_cache_expires(self):
        with patch.object(q, '_runner_budget', return_value=1500), patch.object(q.requests, 'get', return_value=self.response({'jobs': []})) as get:
            q._runner_run_jobs(42, {})
            stamp, response = q.runner_reads[('jobs', 42)]
            q.runner_reads[('jobs', 42)] = (stamp - 121, response)
            q._runner_run_jobs(42, {})
            self.assertEqual(get.call_count, 2)

if __name__ == '__main__':
    unittest.main()
