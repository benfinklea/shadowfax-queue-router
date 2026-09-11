"""Evidence captures for the machine/robot/chest shapes + Factorio sprites +
animated assembler + long-handed-inserter arrows (Ben, 3:16-3:25 PM CDT
2026-09-11). Fixture-driven (no live GitHub/SSH creds needed in CI), same
pattern as cicd-layout.py: stub window.fetch, load the ship-flow JS out of the
real page HTML, drive it, screenshot.
"""
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import queue_router as q

OUT = ROOT / 'evidence/factorio'
OUT.mkdir(parents=True, exist_ok=True)

client = q.app.test_client()
html = client.get('/').get_data(as_text=True)
styles = '\n'.join(re.findall(r'<style>(.*?)</style>', html, re.S))
body = html[html.index('<body>'):html.index('<div class="card panel-refresh-surface" id="monitors-panel"')] + '</div></body>'
help_text = html[html.index('const HELP = {'):html.index('\n};', html.index('const HELP = {')) + 3]
ship = html[html.index('function sparkHtml('):html.index('function refreshCiQueue(')]

# A fully-populated 15-stage payload: every stage has a live count so every
# machine/robot/chest shape, every sprite, and a running assembler all render
# at once for the evidence shot.
pipeline_fixture = dict(
    available=True, degraded=False, repo='armbrain-io/armbrain',
    bugs_found_24h=3, last_issue_created_at='2026-09-11T19:40:00Z',
    issues_open=7,
    dispatched=2, dispatched_last_at='2026-09-11T20:10:00Z',
    prs_open=4, prs_open_last_at='2026-09-11T20:15:00Z',
    ci_queued=1, ci_running=2, ci_last_run_started_at='2026-09-11T20:20:00Z',
    review_routed=2, review_routed_last_at='2026-09-11T20:05:00Z',
    in_review=1, in_review_last_at='2026-09-11T20:12:00Z',
    gate_verdicts=1, gate_verdicts_last_at='2026-09-11T20:14:00Z',
    conflicted=1, conflicted_last_at='2026-09-11T19:50:00Z',
    resolved=2, resolved_na_reason=None,
    approved=2, approved_last_at='2026-09-11T20:00:00Z',
    queue_depth=3, queue_prs=[], queue_sub='',
    merged_today=5, merged_last_hour=2, merged_spark=[1, 2, 1, 3, 2],
    merged_today_prs=[], last_merge_at='2026-09-11T20:18:00Z',
    folded=1, folded_last_at='2026-09-11T20:19:00Z',
    deploys_ok_today=1, deploys_in_flight=1,
    last_deploy_sha='b494d7e', last_deploy_at='2026-09-11T20:20:00Z',
    green_waiting=0, green_waiting_prs=[],
)
agents_fixture = [
    {'live': True, 'square': 'issues open'},
    {'live': True, 'square': 'dispatched'},
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('http://127.0.0.1:5099/', wait_until='domcontentloaded')
    page.evaluate(
        '(d)=>{window.data=d;window.fetch=async(url)=>({ok:true,json:async()=>window.data[url]});}',
        {'/api/agents': agents_fixture, '/api/pipeline': pipeline_fixture},
    )
    page.evaluate('async()=>{await refreshShipFlow();}')
    page.wait_for_timeout(1200)
    assert not errors, errors
    assert page.locator('.ship-stage.stage-machine').count() > 0
    assert page.locator('.ship-stage.stage-robot').count() > 0
    assert page.locator('.ship-stage.stage-chest').count() > 0
    assert page.locator('.ship-legend').count() == 1

    strip = page.locator('#ship-flow')
    strip.screenshot(path=str(OUT / 'strip-1440-full.png'))

    page.evaluate("document.documentElement.style.filter='grayscale(100%)'")
    page.wait_for_timeout(150)
    strip.screenshot(path=str(OUT / 'strip-1440-greyscale.png'))
    page.evaluate("document.documentElement.style.filter=''")

    # Prove the strip still renders if the sprite directory disappears at
    # runtime (Ben's "must still render" requirement) - point every sprite at
    # a guaranteed-missing path and re-capture.
    page.evaluate("""
        document.querySelectorAll('.ship-sprite').forEach(el => {
            el.style.backgroundImage = "url('/static/factorio-MISSING/none.png')";
        });
    """)
    page.wait_for_timeout(150)
    strip.screenshot(path=str(OUT / 'strip-1440-sprites-missing.png'))

    browser.close()

print('OK', OUT)
