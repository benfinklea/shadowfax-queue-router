"""Agent liveness, mapping, cache and failure contracts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import queue_router as q


class AgentsTest(unittest.TestCase):
    def test_root_descendant_idle_and_zombie(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / 'uptime').write_text('1000 1000')
            for pid, parent, name, state in [(10, 1, 'codex', 'S'), (20, 1, 'bash', 'S'),
                                            (21, 20, 'node', 'S'), (30, 1, 'bash', 'S'),
                                            (40, 1, 'claude', 'Z')]:
                (proc / str(pid)).mkdir()
                fields = [state, str(parent)] + ['0'] * 17 + ['100']
                (proc / str(pid) / 'stat').write_text(f'{pid} ({name}) ' + ' '.join(fields))
            windows = '\n'.join(f'codex\tissue-{pid}\t{pid}' for pid in (10, 20, 30, 40))
            with patch.object(q, 'Path', side_effect=lambda value: proc if value == '/proc' else proc / 'uptime'), patch.object(q.subprocess, 'run') as run:
                run.return_value.stdout = windows
                rows = q._agent_windows()
            self.assertEqual([r['live'] for r in rows], [True, True, False, False])
            self.assertTrue(rows[0]['started_at'])
            self.assertIsNone(rows[2]['started_at'])

    def lane(self, number=12, repo=None):
        return dict(live=True, kind='codex-lane', target=dict(repo=repo or q.GITHUB_CI_REPO, issue=number, number=number), square='fleet')

    def test_issue_reference_boundaries_and_repo_scope(self):
        for text, expected in [('Fix #12', 'prs open'), ('Fix issue-12', 'prs open'),
                               ('Fix #123', 'issues open'), ('Fix issue-123', 'issues open'),
                               ('other/repo#12', 'issues open')]:
            with self.subTest(text=text), patch.object(q, '_agents_gh', return_value={'title': 'Issue', 'state': 'open'}), patch.object(q, '_agent_prs', return_value=[dict(number=7, title=text, body='')]):
                self.assertEqual(q._map_agents([self.lane()])[0]['square'], expected)
        with patch.object(q, '_agents_gh', return_value={'title': 'Issue', 'state': 'open'}), patch.object(q, '_agent_prs', return_value=[]):
            self.assertEqual(q._map_agents([self.lane(repo='armbrain-io/fleet-planning')])[0]['square'], 'fleet')

    def test_shepherd_precedence_and_strip_readiness(self):
        base = dict(title='PR', state='OPEN', headRefOid='abc', isDraft=False,
                    reviewDecision='APPROVED', mergeable='MERGEABLE', mergeStateStatus='CLEAN', labels=[])
        for queued, running, changes, expected in [
            (True, True, {}, 'in line'), (False, True, {}, 'ci q/run'),
            (False, False, {}, 'green waiting'), (False, False, {'mergeStateStatus':'BLOCKED'}, 'prs open'),
            (False, False, {'isDraft':True}, 'prs open'),
            (False, False, {'labels':[{'name':'hold'}]}, 'prs open'),
            (False, False, {'state':'MERGED'}, 'fleet')]:
            responses = [dict(base, **changes), {'data':{'repository':{'pullRequest':{'mergeQueueEntry':{'id':'q'} if queued else None}}}}, {'total_count':int(running)}]
            row = dict(live=True, kind='shepherd', target=dict(repo=q.GITHUB_CI_REPO, pr=7, number=7), square='fleet')
            with self.subTest(expected=expected, changes=changes), patch.object(q, '_agents_gh', side_effect=responses):
                self.assertEqual(q._map_agents([row])[0]['square'], expected)

    def test_failed_mapping_is_unknown(self):
        with patch.object(q, '_agents_gh', side_effect=ValueError('offline')):
            row = q._map_agents([self.lane()])[0]
            self.assertTrue(row['live'])
            self.assertIsNone(row['square'])
            self.assertIn('mapping_error', row)

    def test_cache_and_failed_census(self):
        with patch.dict(q.agents_cache, data=None, ts=0), patch.object(q, '_agent_windows', return_value=[]) as census, patch.object(q, '_map_agents', side_effect=lambda rows: rows):
            q.get_agents(); q.get_agents()
            self.assertEqual(census.call_count, 1)
            q.get_agents(force_refresh=True)
            self.assertEqual(census.call_count, 2)
        with patch.object(q, 'get_agents', side_effect=RuntimeError('tmux unavailable')):
            response = q.app.test_client().get('/api/agents')
            self.assertEqual(response.status_code, 503)
            self.assertIn('error', response.get_json())

    def test_repo_selection_scopes_agents_and_yard_workers(self):
        repos = ('armbrain-io/armbrain', 'armbrain-io/fleet-planning')
        rows = [
            dict(window=f'WORK-shepherd-{number}', session='workers', kind='shepherd',
                 live=True, square='prs open', host='gandalf', started_at=None,
                 target=dict(repo=repo, pr=number, number=number))
            for number, repo in enumerate(repos, start=41)
        ]
        client = q.app.test_client()
        known = [dict(name=repo.rsplit('/', 1)[1], full_name=repo, default_branch='main')
                 for repo in repos]
        q._ship_worker_state.clear()
        q._ship_completions.clear()
        with patch.dict(q.agents_cache, data=None, ts=0), \
                patch.object(q, '_agent_windows', return_value=rows), \
                patch.object(q, '_map_agents', side_effect=lambda agents: agents), \
                patch.object(q, '_get_repo_list', return_value=known), \
                patch.object(q, 'get_pipeline_status_fast', return_value={'available': True}), \
                patch.object(q, 'get_ci_queue_status_fast', return_value={'available': False}), \
                patch.object(q, '_get_arrow_rates_fast', return_value={}), \
                patch.object(q, '_build_ship_arrows', return_value=[]), \
                patch.object(q, '_get_ship_spark12h', return_value={}):
            for selected, expected_window in zip(('armbrain', 'fleet-planning'),
                                                 ('WORK-shepherd-41', 'WORK-shepherd-42')):
                response = client.get(f'/api/agents?repo={selected}')
                self.assertEqual(response.status_code, 200)
                agents = response.get_json()
                workers, _ = q._build_ship_workers_and_completions(
                    agents, repo=f'armbrain-io/{selected}')
                self.assertEqual([agent['window'] for agent in agents], [expected_window])
                self.assertEqual([worker['window'] for worker in workers], [expected_window])
                yard = client.get(f'/api/pipeline?repo={selected}').get_json()
                self.assertEqual([worker['window'] for worker in yard['workers']],
                                 [expected_window])
            self.assertEqual(client.get('/api/agents?repo=not-tracked').status_code, 404)


if __name__ == '__main__':
    unittest.main()
