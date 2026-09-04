"""HOME LAB inventory must exclude cloud machines across every linked page."""
import unittest
from unittest.mock import Mock, patch
import requests
import queue_router as q


def runner(name, labels=(), status='online', busy=False):
    return dict(name=name, labels=[{'name': label} for label in labels], status=status, busy=busy)


def response(runners, next_url=None):
    return Mock(status_code=200, json=lambda: {'runners': runners},
                links={'next': {'url': next_url}} if next_url else {})


class LocalRunnersTest(unittest.TestCase):
    def setUp(self):
        q.local_runners_cache.update(data=None, ts=0)

    def tearDown(self):
        q.local_runners_cache.update(data=None, ts=0)

    def test_link_pagination_filter_hosts_and_cache(self):
        next_url = 'https://api.github.com/orgs/armbrain-io/actions/runners?per_page=100&page=2'
        pages = [response([runner('runs-on--cloud')]*100, next_url), response([
            runner('cloud-by-label', ['runs-on=123/runner=2cpu']),
            runner('worker-1', ['northfarthing', 'fellowship-gate'], busy=True),
            runner('northfarthing-runner-2', ['fellowship-gate']),
            runner('northfarthing-runner-3', status='offline', busy=True),
            runner('gate-worker', ['fellowship-gate']),
            runner('unknown-host', status='offline')])]
        with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=pages) as get:
            result = q.app.test_client().get('/api/local_runners').get_json()
            self.assertEqual((result['online'], result['busy'], result['idle']), (3, 1, 2))
            self.assertEqual(result['excluded_cloud'], 101)
            self.assertEqual(result['source'], 'org_inventory')
            self.assertEqual(get.call_args_list[0].args[0], 'https://api.github.com/orgs/armbrain-io/actions/runners?per_page=100')
            self.assertEqual(len(result['runners']), 5)
            hosts = {h['host']: h for h in result['hosts']}
            self.assertEqual((hosts['northfarthing']['online'], hosts['northfarthing']['busy'], hosts['northfarthing']['idle']), (2, 1, 1))
            self.assertEqual(hosts['unknown-host']['online'], 0)
            self.assertEqual(get.call_args_list[1].args[0], next_url)
            self.assertEqual(q.get_local_runners(), result)
            self.assertEqual(get.call_count, 2)

    def test_short_page_with_next_link_and_empty_inventory(self):
        with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=[response([runner('runs-on--cloud')], 'https://api.github.com/next'), response([])]) as get:
            result = q.get_local_runners(True)
            self.assertEqual(get.call_count, 2)
            self.assertTrue(result['available'])
            self.assertEqual(result['online'], 0)
            self.assertEqual(result['hosts'], [])
            self.assertEqual(result['excluded_cloud'], 1)

    def test_partial_inventory_is_never_reported_as_complete(self):
        for failure in [Mock(status_code=404), requests.Timeout('test')]:
            with self.subTest(failure=failure), patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=[response([runner('sam-1')], 'https://api.github.com/next'), failure]):
                result = q.get_local_runners(True)
                self.assertFalse(result['available'])
                self.assertNotIn('online', result)
                self.assertNotIn('runners', result)


class JobHistoryTest(unittest.TestCase):
    setUp = LocalRunnersTest.setUp
    tearDown = LocalRunnersTest.tearDown
    @staticmethod
    def page(key, values, next_url=None):
        return Mock(status_code=200, json=lambda: {key: values},
                    links={'next': {'url': next_url}} if next_url else {})

    def run_record(self, id, status='completed', minutes=5):
        return dict(id=id, status=status, updated_at=(q.datetime.now(q.timezone.utc) - q.timedelta(minutes=minutes)).isoformat())

    def test_fallback_deduplicates_filters_and_keeps_unknowns(self):
        active = self.run_record(1, 'in_progress')
        recent = self.run_record(2)
        old = self.run_record(3, minutes=61)
        def job(name, status='completed', labels=()):
            return dict(runner_name=name, status=status, labels=labels)
        pages = [Mock(status_code=403), self.page('workflow_runs', [active]),
                 self.page('workflow_runs', [recent, active, old]),
                 self.page('jobs', [job('aragorn-5', 'in_progress'), job('runs-on--cloud', 'in_progress'),
                                    job('cloud-label', 'in_progress', ['runs-on=abc']), job('', 'queued')], 'https://api.github.com/jobs-next'),
                 self.page('jobs', [job('eastfarthing-3')]),
                 self.page('jobs', [job('aragorn-5'), job('eastfarthing-3')])]
        with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=pages) as get:
            result = q.get_local_runners(True)
            self.assertEqual(result['source'], 'job_history')
            self.assertTrue(result['available'])
            self.assertEqual((result['busy'], result['seen_last_hour']), (1, 2))
            self.assertIsNone(result['online'])
            self.assertIsNone(result['idle'])
            self.assertEqual(result['excluded_cloud'], 2)
            self.assertEqual([h['host'] for h in result['hosts']], ['aragorn', 'eastfarthing'])
            self.assertTrue(all(h['online'] is None and h['idle'] is None for h in result['hosts']))
            self.assertEqual(result['runs_checked'], 2)
            self.assertEqual(q.get_local_runners(), result)
            self.assertEqual(get.call_count, 6)

    def test_run_cap_prioritizes_active_and_flags_truncation(self):
        recent = [self.run_record(i) for i in range(35)]
        active = self.run_record(99, 'in_progress', minutes=50)
        pages = [Mock(status_code=403), self.page('workflow_runs', [active]),
                 self.page('workflow_runs', recent)] + [self.page('jobs', []) for _ in range(30)]
        with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=pages) as get:
            result = q.get_local_runners(True)
            self.assertEqual(get.call_count, 33)
            self.assertIn('/runs/99/jobs?', get.call_args_list[3].args[0])
            self.assertEqual(result['runs_checked'], 30)
            self.assertTrue(result['truncated'])
            self.assertEqual(result['seen_last_hour'], 0)
            self.assertEqual(result['busy'], 0)

    def test_failed_jobs_do_not_publish_partial_counts_and_cache_error(self):
        pages = [Mock(status_code=403), self.page('workflow_runs', []),
                 self.page('workflow_runs', [self.run_record(1)]), requests.Timeout('jobs timeout')]
        with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=pages) as get:
            result = q.get_local_runners(True)
            self.assertFalse(result['available'])
            self.assertEqual(result['source'], 'job_history')
            self.assertNotIn('busy', result)
            self.assertEqual(q.get_local_runners(), result)
            self.assertEqual(get.call_count, 4)
