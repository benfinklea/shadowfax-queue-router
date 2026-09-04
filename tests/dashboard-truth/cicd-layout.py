"""Scratch-only API checks and 1440px captures; no service startup or restart."""
import json
import re
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch, Mock
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import queue_router as q
OUT = ROOT / 'evidence/cicd-layout'
OUT.mkdir(parents=True, exist_ok=True)
client = q.app.test_client()
# Check paging, counts, cache, and permission denial without inventing live counts.
runner = dict(name='farthing-test', labels=[{'name':'fellowship-gate'}, {'name':'ARM64'}], status='online', busy=True)
with patch.object(q, 'get_gh_ci_token', return_value='test'), patch.object(q.requests, 'get') as get:
    get.side_effect = [Mock(status_code=200, links={'next': {'url': 'https://api.github.com/repos/test/actions/runners?per_page=100&page=2'}}, json=lambda:{'runners':[runner]*100}), Mock(status_code=200, links={}, json=lambda:{'runners':[]})]
    known = q.get_local_runners(True)
    assert known['online'] == known['busy'] == 100 and known['idle'] == 0
    assert q.get_local_runners() == known and get.call_count == 2
    for status in [404]:
        get.side_effect = None
        get.return_value = Mock(status_code=status)
        denied = q.get_local_runners(True)
        assert denied['error'] == 'no permission to read runners' and 'online' not in denied
q.local_runners_cache.update(data=None, ts=0)
live_local = client.get('/api/local_runners').get_json()
# Re-read /api/agents for the arrow evidence.
agents_response = client.get('/api/agents?fresh=1')
agents = agents_response.get_json()
if not isinstance(agents,list): agents = None
pipeline = client.get('/api/pipeline?fresh=1').get_json()
assert pipeline['available'], pipeline
aws = client.get('/api/runson').get_json()
html = client.get('/').get_data(as_text=True)
styles = '\n'.join(re.findall(r'<style>(.*?)</style>',html,re.S))
body = html[html.index('<body>'):html.index('<div class="card panel-refresh-surface" id="monitors-panel"')] + '</div></body>'
help_text = html[html.index('const HELP = {'):html.index('\n};',html.index('const HELP = {'))+3]
ship = html[html.index('function sparkHtml('):html.index('function refreshCiQueue(')]
runson = html[html.index('function runsonEscape('):html.index('// --- Polling control:')]
counts = Counter(a['square'] for a in agents or [] if a['live'])
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440,'height':800})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.set_content('<style>'+styles+'</style>'+body)
    page.evaluate('(d)=>{window.data=d;window.fetch=async(url)=>({ok:true,json:async()=>window.data[url]});}', {'/api/agents':agents,'/api/pipeline':pipeline,'/api/runson':aws,'/api/local_runners':live_local})
    page.add_script_tag(content=help_text+'\n'+ship+'\n'+runson)
    page.evaluate('async()=>{await refreshShipFlow();refreshRunsOn();await refreshLocalRunners();}')
    assert page.locator('.ship-cap').all_text_contents() == ['issues open','prs open','ci q/run','green waiting','in line','merged today','last deploy (CT)']
    rects = page.locator('.ship-stage').evaluate_all('(els)=>els.map(e=>{const r=e.getBoundingClientRect();return {y:r.y,right:r.right}})')
    assert len({r['y'] for r in rects}) == 1 and max(r['right'] for r in rects) <= 1440, rects
    assert page.locator('.ship-arrow').count()==6
    for arrow in page.locator('.ship-arrow').all():
        square=arrow.get_attribute('data-square-left')
        assert arrow.inner_text()==('🤖 ?' if agents is None else '🤖'+(' '+str(counts[square]) if counts[square] else ''))
        arrow.click()
        panel=page.locator('#'+arrow.get_attribute('aria-controls'))
        assert panel.is_visible() and panel.locator('.ship-agent-row').count()==counts[square]
        arrow.click()
    assert page.locator('#ci-queue-body,#route-health-body,.ship-label,.ship-agent-chip,.runson-chart').count()==0
    assert page.locator('.cicd-label').inner_text()=='CI/CD'
    page.screenshot(path=str(OUT/'live-1440.png'))
    # Clearly marked controlled data proves states unavailable from live credentials.
    aws_fixture=dict(available=True,deployed=True,live_runners=2,gate_shards='on',spent_today=1.25,spent_month=40,credits_remaining=None,credits_error='Credits API permission unavailable')
    known.update(runners=[runner | {'labels':['fellowship-gate','farthing-test','ARM64']}],online=1,busy=1,idle=0)
    page.evaluate('(d)=>{data["/api/runson"]=d.aws;data["/api/local_runners"]=d.local;refreshRunsOn();return refreshLocalRunners();}',dict(aws=aws_fixture,local=known))
    facts=page.locator('#runson-budget-strip > div')
    assert facts.count()==3
    assert len(set(facts.evaluate_all('(els)=>els.map(e=>e.getBoundingClientRect().y)')))==1
    assert 'Credits API permission unavailable' in facts.first.inner_text()
    assert 'fellowship-gate' in page.locator('#local-runners-body details').text_content()
    page.locator('#local-runners-body summary').click()
    page.screenshot(path=str(OUT/'available-fixture-1440.png'))
    page.evaluate('(d)=>{data["/api/local_runners"]=d;return refreshLocalRunners();}',denied)
    assert 'no permission to read runners' in page.locator('#local-runners-body').inner_text()
    assert page.locator('#local-runners-body .runner-facts').count()==0
    page.screenshot(path=str(OUT/'permission-fixture-1440.png'))
    for rows in [[],None]:
        page.evaluate('(rows)=>{data["/api/agents"]=rows;return refreshShipFlow();}', rows)
        assert page.locator('.ship-arrow').all_text_contents()==(['🤖']*6 if rows==[] else ['🤖 ?']*6)
    assert not errors,errors
    browser.close()
(OUT/'truth.json').write_text(json.dumps(dict(viewport=1440,stage_rects=rects,agents_available=agents is not None,per_square=dict(counts),local_runners=live_local,aws_available=aws.get('available'),checks='PASS: order, no wrap, live arrow counts and dropdowns, zero/unknown arrows, removed blocks, three facts, local available/permission states, pagination/cache/403/404',errors=errors),indent=2)+'\n')
print((OUT/'truth.json').read_text())
