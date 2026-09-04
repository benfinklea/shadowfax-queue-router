"""Shipping readiness excludes held/queued work and refuses incomplete reads."""
import json
import subprocess
import unittest
from unittest.mock import patch

import queue_router as qr


class GreenWaitingTest(unittest.TestCase):
    def read(self, prs, queue):
        replies = [subprocess.CompletedProcess([], 0, json.dumps(prs)),
                   subprocess.CompletedProcess([], 0, json.dumps({'data': {'repository': {'mergeQueue': queue}}}))]
        with patch.object(qr.subprocess, 'run', side_effect=replies):
            return qr._get_shipping_readiness('test-token')

    def test_exclusions_and_oldest_order(self):
        base = dict(isDraft=False, reviewDecision='APPROVED', mergeable='MERGEABLE', mergeStateStatus='CLEAN', labels=[])
        prs = [dict(base, number=i, title=f'PR {i}') for i in range(1, 12)]
        prs[1]['isDraft'] = True
        prs[2]['reviewDecision'] = 'REVIEW_REQUIRED'
        prs[3]['mergeable'] = 'CONFLICTING'
        prs[4]['mergeable'] = 'UNKNOWN'
        for pr, label in zip(prs[5:9], ['do-not-merge', 'needs-repair', 'hold', 'blocked-on-ben']):
            pr['labels'] = [{'name': label}]
        queue = {'entries': {'nodes': [{'state': 'QUEUED', 'pullRequest': {'number': 10, 'title': 'PR 10'}}], 'pageInfo': {'hasNextPage': False}}}
        self.assertEqual(self.read(prs, queue)[0], [{'number': 1, 'title': 'PR 1'}, {'number': 11, 'title': 'PR 11'}])

    def test_only_clean_checks_are_green(self):
        prs = [dict(number=i, title=state, isDraft=False, reviewDecision='APPROVED',
                    mergeable='MERGEABLE', mergeStateStatus=state, labels=[])
               for i, state in enumerate(['CLEAN', 'BLOCKED', 'UNSTABLE', 'UNKNOWN', 'BEHIND'])]
        queue = {'entries': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}
        self.assertEqual(self.read(prs, queue)[0], [{'number': 0, 'title': 'CLEAN'}])

    def test_queue_preserves_front_order_and_entry_states(self):
        entries = [{'state': state, 'pullRequest': {'number': number, 'title': f'PR {number}'}}
                   for number, state in [(42, 'AWAITING_CHECKS'), (7, 'UNMERGEABLE')]]
        waiting, queue = self.read([], {'entries': {'nodes': entries, 'pageInfo': {'hasNextPage': False}}})
        self.assertEqual(waiting, [])
        self.assertEqual(queue, [{'number': 42, 'title': 'PR 42', 'state': 'AWAITING_CHECKS'},
                                 {'number': 7, 'title': 'PR 7', 'state': 'UNMERGEABLE'}])

    def test_missing_or_truncated_queue_is_not_zero(self):
        for queue in [None, {'entries': {'nodes': [], 'pageInfo': {'hasNextPage': True}}}]:
            with self.subTest(queue=queue), self.assertRaises(ValueError):
                self.read([], queue)

    def test_empty_queue_and_prs_is_zero(self):
        self.assertEqual(self.read([], {'entries': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}), ([], []))


if __name__ == '__main__':
    unittest.main()
