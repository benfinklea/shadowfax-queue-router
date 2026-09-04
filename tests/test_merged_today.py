"""Complete merged search, merge ordering, and incomplete-read rejection."""
import unittest
from unittest.mock import Mock, patch
import queue_router as qr


class MergedTodayTest(unittest.TestCase):
    def response(self, numbers, total=None, incomplete=False):
        return Mock(json=lambda: {
            'total_count': len(numbers) if total is None else total,
            'incomplete_results': incomplete,
            'items': [{'number': n, 'title': f'PR {n}',
                       'pull_request': {'merged_at': f'2026-09-04T{n:02}:00:00Z'}} for n in numbers],
        })

    def test_pagination_and_merge_time_order(self):
        with patch.object(qr.requests, 'get', side_effect=[self.response([1, 3], 3), self.response([2], 3)]) as get:
            self.assertEqual(qr._get_merged_today_prs({}, 'today'),
                             [{'number': n, 'title': f'PR {n}'} for n in [3, 2, 1]])
            self.assertEqual([c.kwargs['params']['page'] for c in get.call_args_list], [1, 2])

    def test_empty(self):
        with patch.object(qr.requests, 'get', return_value=self.response([])):
            self.assertEqual(qr._get_merged_today_prs({}, 'today'), [])

    def test_incomplete_or_truncated_or_duplicate(self):
        for response in [self.response([1], incomplete=True), self.response([], 1001),
                         self.response([], 2), self.response([1, 1])]:
            with self.subTest(response=response), patch.object(qr.requests, 'get', return_value=response):
                with self.assertRaises(ValueError):
                    qr._get_merged_today_prs({}, 'today')

    def test_http_failure(self):
        response = self.response([])
        response.raise_for_status.side_effect = qr.requests.HTTPError('unavailable')
        with patch.object(qr.requests, 'get', return_value=response):
            with self.assertRaises(qr.requests.HTTPError):
                qr._get_merged_today_prs({}, 'today')
