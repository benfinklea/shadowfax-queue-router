"""Evidence captures for LORE order 11 ("the sprite is the square"): card
removal, real belts carrying the order-10 item chain, and rate-driven
animation. Same fixture/stub-fetch pattern as factorio-evidence.py.

Requires a live copy of this app on 127.0.0.1:5099 (see evidence/factorio-belts/
README.md for the one-line startup command) - not started here, same as the
existing factorio-evidence.py, so this can run against either a fixture-only
sandbox or a fully-configured checkout.
"""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'evidence' / 'factorio-belts'
OUT.mkdir(parents=True, exist_ok=True)

# A fully-populated 15-stage payload, with one deliberately measured-zero/
# backlogged arrow (ci-green -> the bottleneck) so a backed-up belt renders
# packed and red, and one deliberately no-data square (prs_open=None) so the
# assembler's remnant wreckage renders instead of a live sprite.
pipeline_fixture = dict(
    available=True, degraded=False, repo='armbrain-io/armbrain',
    bugs_found_24h=3, last_issue_created_at='2026-09-11T19:40:00Z',
    issues_open=7,
    dispatched=2, dispatched_last_at='2026-09-11T20:10:00Z',
    prs_open=None, prs_open_last_at=None,
    ci_queued=1, ci_running=2, ci_last_run_started_at='2026-09-11T18:00:00Z',
    review_routed=2, review_routed_last_at='2026-09-11T20:05:00Z',
    in_review=1, in_review_last_at='2026-09-11T20:12:00Z',
    gate_verdicts=None, gate_verdicts_last_at=None,
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
    arrows=[
        {'key': 'issues-prs', 'rate_per_hour': 6, 'backlog': 4, 'drain_hours': 0.7},
        {'key': 'prs-ci', 'rate_per_hour': 3, 'backlog': 2, 'drain_hours': 0.7},
        {'key': 'ci-green', 'rate_per_hour': 0, 'backlog': 5, 'drain_hours': None},
        {'key': 'green-inline', 'rate_per_hour': 2, 'backlog': 1, 'drain_hours': 0.5},
        {'key': 'inline-merged', 'rate_per_hour': 4, 'backlog': 3, 'drain_hours': 0.75},
        {'key': 'merged-deploy', 'rate_per_hour': 1, 'backlog': 0, 'drain_hours': 0},
    ],
)
agents_fixture = [
    {'live': True, 'square': 'issues open'},
    {'live': True, 'square': 'dispatched'},
]

def stub_routes(page):
    # Network-level route interception, registered BEFORE navigation, so it
    # cannot race the page's own on-load fetch the way a post-hoc
    # window.fetch monkeypatch can (this app also has a real, working
    # /api/pipeline in this sandbox - a real background fetch racing in
    # after a same-tick fetch stub was installed was observed to
    # occasionally overwrite the fixture render mid-test).
    page.route('**/api/pipeline*', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(pipeline_fixture)))
    page.route('**/api/agents*', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(agents_fixture)))


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    stub_routes(page)
    page.goto('http://127.0.0.1:5099/', wait_until='domcontentloaded')
    page.evaluate('async()=>{await refreshShipFlow();}')
    page.wait_for_timeout(1500)
    assert not errors, errors

    strip = page.locator('#ship-flow')
    strip.screenshot(path=str(OUT / 'strip-1440-full.png'))

    assert page.locator('.ship-stage').count() == 15
    # Card removal (order 11 #1): no visible border/background left on the box.
    assert page.eval_on_selector('.ship-stage', "el => getComputedStyle(el).borderStyle") == 'none'
    # Belts + items (order 11 #2, order 10): every non-row-end arrow carries a
    # real belt with at least one item; the deliberately-stalled ci-green
    # arrow's belt is packed/backed-up.
    assert page.locator('.ship-belt').count() == 12
    assert page.locator('.ship-belt-item').count() > 0
    assert page.locator('.ship-belt-backed-up').count() == 1
    # No-data state (prs open) shows real remnant wreckage, not a live sprite.
    assert page.locator('[data-square="prs open"] .ship-sprite.remnant').count() == 1

    # Three states side by side, proven never to look alike.
    page.locator('[data-square="bugs found"]').screenshot(path=str(OUT / 'state-idle.png'))
    page.locator('[data-square="ci q/run"]').screenshot(path=str(OUT / 'state-stalled.png'))
    page.locator('[data-square="prs open"]').screenshot(path=str(OUT / 'state-no-data.png'))

    # Greyscale fallback - the backed-up belt must still read as packed/dense.
    page.evaluate("document.documentElement.style.filter='grayscale(100%)'")
    page.wait_for_timeout(150)
    strip.screenshot(path=str(OUT / 'strip-1440-greyscale.png'))
    page.evaluate("document.documentElement.style.filter=''")

    browser.close()

# Reduced-motion pass, in a fresh context (emulate_media must be set before
# navigation to take effect on first paint).
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.emulate_media(reduced_motion='reduce')
    stub_routes(page)
    page.goto('http://127.0.0.1:5099/', wait_until='domcontentloaded')
    page.evaluate('async()=>{await refreshShipFlow();}')
    page.wait_for_timeout(500)
    assert page.eval_on_selector('.ship-belt-track', "el => getComputedStyle(el).animationName") == 'none'
    assert page.eval_on_selector('.ship-arrow.bottleneck', "el => getComputedStyle(el).animationName") == 'none'
    page.locator('#ship-flow').screenshot(path=str(OUT / 'strip-1440-reduced-motion.png'))
    browser.close()

print('OK', OUT)
