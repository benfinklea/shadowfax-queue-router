"""Read-only truth suite and headless capture of the scratch app, never the daemon.
Run: /tmp/green-waiting-venv/bin/python tests/dashboard-truth/agents-badge.py
Requires gh auth, access to Ben's live tmux server and Playwright Chromium.
"""
import base64
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import queue_router as q

OUT = ROOT / 'evidence/agents-badge'
OUT.mkdir(parents=True, exist_ok=True)

def run(args):
    return subprocess.check_output(args, text=True, timeout=45)

def gh(*args):
    return json.loads(run(['gh', *args]))

def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n')

client = q.app.test_client()
response = client.get('/api/agents?fresh=1')
assert response.status_code == 200, response.get_json()
agents = response.get_json()
save('agents.json', agents)
# Independent fresh list-windows + process census: climb each worker's ancestry
# toward each pane root (including the worker itself), rather than reusing code.
windows = run(['tmux', '-S', f'/tmp/tmux-{os.getuid()}/default', 'list-windows', '-a', '-F', '#{session_name}\t#{window_name}\t#{pane_pid}'])
(OUT / 'tmux-windows.txt').write_text(windows)
processes = {}
for line in run(['ps', '-eo', 'pid=,ppid=,stat=,comm=']).splitlines():
    pid, parent, state, name = line.strip().split(None, 3)
    try:
        exe = Path(os.readlink(f'/proc/{pid}/exe')).name
    except OSError:
        exe = name
    processes[int(pid)] = (int(parent), state[0] not in 'ZX' and bool({name, exe} & {'claude', 'codex', 'node'}))
live_windows = set()
for line in windows.splitlines():
    session, window, root = line.split('\t')
    eligible = ((session == 'codex' and re.fullmatch(r'issue-\d+(-fp)?', window)) or window.startswith('WORK-') or (session == 'rangers' and window.startswith('LANE-')))
    if not eligible or window.endswith('-MAIN'):
        continue
    for pid, (_, worker) in processes.items():
        if not worker:
            continue
        seen = set()
        while pid and pid not in seen:
            if pid == int(root):
                live_windows.add((session, window))
                break
            seen.add(pid)
            pid = processes.get(pid, (0, False))[0]
api_live = {(a['session'], a['window']) for a in agents if a['live']}
assert api_live == live_windows, (api_live, live_windows)
checks = []
for agent in agents:
    if not agent['live'] or not agent['target']:
        assert agent['square'] == 'fleet'
        continue
    target = agent['target']
    repo, number = target['repo'], target['number']
    if 'pr' in target:
        owner, name = repo.split('/')
        query = '''query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){state headRefOid isDraft reviewDecision mergeable mergeStateStatus labels(first:100){nodes{name}} mergeQueueEntry{id}}}}'''
        pr = gh('api', 'graphql', '-f', 'query=' + query, '-f', 'o=' + owner, '-f', 'r=' + name, '-F', 'n=' + str(number))['data']['repository']['pullRequest']
        runs = gh('api', f'repos/{repo}/actions/runs?head_sha={pr["headRefOid"]}&status=in_progress&per_page=1')
        held = {'do-not-merge', 'needs-repair', 'hold', 'blocked-on-ben'}
        ready = not pr['isDraft'] and pr['reviewDecision'] == 'APPROVED' and pr['mergeable'] == 'MERGEABLE' and pr['mergeStateStatus'] == 'CLEAN' and not held.intersection(x['name'].lower() for x in pr['labels']['nodes'])
        expected = ('fleet' if pr['state'] != 'OPEN' else 'in line' if pr['mergeQueueEntry'] else 'ci q/run' if runs['total_count'] else 'green waiting' if ready else 'prs open')
        checks.append(dict(target=target, expected=expected, github=pr, running=runs['total_count']))
    else:
        issue = gh('api', f'repos/{repo}/issues/{number}')
        pages = gh('api', '--paginate', '--slurp', f'repos/{repo}/pulls?state=open&per_page=100')
        refs = [pr['number'] for page in pages for pr in page if re.search(rf'(?<![\w/])(?:#{number}|issue-{number})(?![\w-])', pr['title'] + '\n' + (pr['body'] or ''))]
        expected = ('fleet' if repo != q.GITHUB_CI_REPO or (issue['state'] != 'open' and not refs) else 'prs open' if refs else 'issues open')
        checks.append(dict(target=target, expected=expected, open_pr_references=refs))
    assert agent['square'] == expected, (agent, expected)

pipeline = client.get('/api/pipeline?fresh=1').get_json()
assert pipeline['available'], pipeline
save('pipeline.json', pipeline)
html = client.get('/').get_data(as_text=True)
renderer = html[html.index('function sparkHtml('):html.index('function refreshCiQueue(')]
help_text = html[html.index('const HELP = {'):html.index('\n};', html.index('const HELP = {')) + 3]
styles = '\n'.join(re.findall(r'<style>(.*?)</style>', html, re.S))
counts = Counter(a['square'] for a in agents if a['live'] and a['square'] != 'fleet')
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':420})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_content('<style>' + styles + '</style><div id="capture"><div id="ship-flow" class="ship-flow"></div><div id="ship-fleet" class="ship-fleet"></div></div>')
    page.evaluate('''(data) => {window.pipeline=data.pipeline; window.agents=data.agents; window.shipFetchUrls=[];
      window.fetch=async(url)=>{window.shipFetchUrls.push(String(url));return {ok:true,json:async()=>url.startsWith('/api/agents')?window.agents:window.pipeline};};}''', dict(pipeline=pipeline, agents=agents))
    page.add_script_tag(content=help_text + '\n' + renderer)
    page.evaluate("shipCurrentRepo='armbrain'")
    page.evaluate('refreshShipFlow()')
    assert page.evaluate('shipFetchUrls.includes("/api/agents?repo=armbrain")')
    page.evaluate("shipCurrentRepo='fleet-planning'")
    page.evaluate('refreshShipFlow()')
    assert page.evaluate('shipFetchUrls.includes("/api/agents?repo=fleet-planning")')
    page.evaluate("shipCurrentRepo='armbrain'")
    for stage in page.locator('[data-square]').all():
        square = stage.get_attribute('data-square')
        chip = page.locator('.ship-arrow[data-square-left="' + square + '"]')
        assert chip.count() == 1
        assert chip.inner_text() == ('🤖 ' + str(counts[square]) if counts[square] else '🤖')
        if counts[square]:
            assert chip.inner_text() == f'🤖 {counts[square]}'
            chip.click()
            panel = stage.locator('.ship-dropdown')
            assert panel.is_visible()
            assert panel.locator('.ship-agent-row').count() == counts[square]
            for link in panel.locator('.ship-agent-row a').all():
                assert link.get_attribute('target') == '_blank'
                assert link.get_attribute('href').startswith('https://github.com/armbrain-io/')
            chip.click()
    logo = 'data:image/svg+xml;base64,' + base64.b64encode((ROOT / 'armbrain-logo.svg').read_bytes()).decode()
    page.locator('#capture').screenshot(path=str(OUT / 'shipping.png'))
    if page.locator('.ship-arrow').count():
        page.locator('.ship-arrow').first.click()
        page.screenshot(path=str(OUT / 'agents-dropdown.png'))
    # Controlled cases supplement (and never replace) the live evidence above.
    fixtures = [dict(window='issue-12', session='codex', kind='codex-lane', live=True, square=square,
                     target=dict(repo=q.GITHUB_CI_REPO, issue=12, number=12), title='<script>very long malicious title')
                for square in ['issues open','prs open','ci q/run','green waiting','in line','merged today']]
    page.evaluate('(rows)=>{window.agents=rows;return refreshShipFlow()}', fixtures)
    assert page.locator('.ship-arrow').count() == 6
    for chip in page.locator('.ship-arrow').all():
        # Close any preserved open dropdown before exercising the chip.
        page.evaluate('openShipDropdown=null')
        chip.click()
        panel = page.locator('#' + chip.get_attribute('aria-controls'))
        assert panel.is_visible()
        assert panel.locator('.ship-agents').count() == 1
        assert panel.locator('script').count() == 0
        assert len(panel.locator('.ship-agent-row span').inner_text()) == 15
        assert panel.evaluate('(el)=>el.firstElementChild.className') == 'ship-agents'
    page.evaluate('window.agents=[];refreshShipFlow()')
    assert page.locator('.ship-arrow.empty').count() == 6
    assert page.locator('#ship-fleet').inner_text() == '🤖 working: 0 on the strip, 0 standing lanes'
    page.evaluate('window.agents=null;refreshShipFlow()')
    assert 'could not establish' in page.locator('#ship-fleet').inner_text()
    assert not errors, errors
    browser.close()
save('truth.json', dict(checked_at=datetime.now(timezone.utc).isoformat(), source='Scratch Flask API on gandalf; live tmux and fresh read-only GitHub queries', live_count=len(api_live), fresh_tmux_live_count=len(live_windows), live_windows=sorted(live_windows), per_square=dict(counts), target_checks=checks, dom='PASS: repo-scoped agent fetches, exact counts, chip toggles, agent section first, safe new-window links, six-square fixtures, zero and unavailable', runtime_errors=errors))
print(json.dumps(dict(live=len(api_live), fresh_tmux=len(live_windows), per_square=dict(counts), targets=len(checks), dom='PASS')))
