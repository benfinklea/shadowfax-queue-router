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
        next_url = 'https://api.github.com/repos/armbrain-io/armbrain/actions/runners?per_page=100&page=2'
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
        for failure in [Mock(status_code=403), Mock(status_code=404), requests.Timeout('test')]:
            with self.subTest(failure=failure), patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get', side_effect=[response([runner('sam-1')], 'https://api.github.com/next'), failure]):
                result = q.get_local_runners(True)
                self.assertFalse(result['available'])
                self.assertNotIn('online', result)
                self.assertNotIn('runners', result)
