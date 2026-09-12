"""Evidence captures for LORE order 11 ("the sprite is the square"): card
removal, real belts carrying the order-10 item chain, and rate-driven
animation. Same fixture/stub-fetch pattern as factorio-evidence.py.

Also covers order 14 (inserter swing), order 15 (BUGS FOUND biter), and
order 16 (ground/shadow/density realism pass).

Requires a live copy of this app on 127.0.0.1:5099 (see evidence/factorio-belts/
README.md for the one-line startup command) - not started here, same as the
existing factorio-evidence.py, so this can run against either a fixture-only
sandbox or a fully-configured checkout.
"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'evidence' / 'factorio-belts'
OUT.mkdir(parents=True, exist_ok=True)

# A fully-populated 15-stage payload, with one deliberately measured-zero/
# backlogged arrow (ci-green -> the bottleneck) so a backed-up belt renders
# packed and red. gate_verdicts and resolved are the two genuinely
# permanent "no instrument for this, ever" fields (Round 3) - both null,
# both must render 'n/a'. Every other field is a real, present value: this
# fixture is what the blanket "no stage ever renders empty" assertion runs
# against, so it has to reflect a normal, fully-available refresh, not a
# degraded one.
pipeline_fixture = dict(
    available=True, degraded=False, repo='armbrain-io/armbrain',
    bugs_found_24h=3, last_issue_created_at='2026-09-11T19:40:00Z',
    issues_open=7,
    dispatched=2, dispatched_last_at='2026-09-11T20:10:00Z',
    prs_open=4, prs_open_last_at='2026-09-11T20:15:00Z',
    ci_queued=1, ci_running=2, ci_last_run_started_at='2026-09-11T18:00:00Z',
    review_routed=2, review_routed_last_at='2026-09-11T20:05:00Z',
    in_review=1, in_review_last_at='2026-09-11T20:12:00Z',
    gate_verdicts=None, gate_verdicts_last_at=None,
    gate_verdicts_na_reason='statusCheckRollup removed from the bulk PR query',
    conflicted=1, conflicted_last_at='2026-09-11T19:50:00Z',
    resolved=None, resolved_na_reason='no snapshot history of mergeable-state transitions to detect a resolve event',
    approved=2, approved_last_at='2026-09-11T20:00:00Z',
    queue_depth=3, queue_prs=[], queue_sub='',
    merged_today=5, merged_last_hour=2, merged_spark=[1, 2, 1, 3, 2],
    merged_today_prs=[], last_merge_at='2026-09-11T20:18:00Z',
    folded=1, folded_last_at='2026-09-11T20:19:00Z',
    deploys_ok_today=1, deploys_in_flight=1,
    last_deploy_sha='b494d7e', last_deploy_at='2026-09-11T20:20:00Z',
    deployed_prs_today=3, deployed_prs_today_list=[101, 102, 103],
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
# A separate, deliberately-degraded copy - prs_open unavailable this refresh
# (distinct from gate_verdicts/resolved's permanent n/a) - used only to
# capture the no-data/remnant-wreckage evidence shot, kept out of the main
# fixture so it can't collide with the "never empty" assertion above: a
# transient per-refresh instrument failure is a different, real state from
# "we structurally never have this number", and both need their own proof.
pipeline_fixture_no_data = dict(pipeline_fixture, prs_open=None, prs_open_last_at=None)

# Order 17 #1: gate_verdicts/resolved are permanently null in the main
# fixture (Round 3's n/a proof) - correctly, their belts render as 'unknown'
# state with ZERO item slots (never inventing a count), so the main fixture
# can't show every one of the 14 belts carrying an item at once. This
# fixture gives both a real, small count so the full item chain - and the
# "readable bug to rocket" evidence screenshot - can be proven end to end.
pipeline_fixture_full_chain = dict(pipeline_fixture, gate_verdicts=3, resolved=2)

# Order 15: the three biter states, keyed off the SAME thresholds bugsCls
# already uses for the number's own colour (0 neutral, 1-4 warn, >=5 hot) -
# not an independently-picked number. The main fixture above (bugs_found_24h=3)
# is the "small" demo; these three cover corpse/medium/unknown.
pipeline_fixture_bugs_corpse = dict(pipeline_fixture, bugs_found_24h=0)
pipeline_fixture_bugs_medium = dict(pipeline_fixture, bugs_found_24h=6)
pipeline_fixture_bugs_unknown = dict(pipeline_fixture, bugs_found_24h=None, last_issue_created_at=None)

# Order 17 "THE REAL ITEM CHAIN": Ben's own list, one item per arrow across
# the 14 sequential handoffs between the 15 stages - bug first, rocket last.
# Kept here (not just read from queue_router.py) so the test is an
# independent check of the mapping, not a copy that could drift silently.
EXPECTED_CHAIN = [
    ('bugs found', 'biter/small-biter.png'),
    ('issues open', 'items/lab.png'),
    ('dispatched', 'items/copper-ore.png'),
    ('prs open', 'items/copper-plate.png'),
    ('ci q/run', 'items/copper-cable.png'),
    ('review routed', 'items/electronic-circuit.png'),
    ('in review', 'items/advanced-circuit.png'),
    ('gate verdicts', 'items/speed-module.png'),
    ('conflicted', 'items/speed-module-2.png'),
    ('resolved', 'items/speed-module-3.png'),
    ('approved', 'items/processing-unit.png'),
    ('in line', 'items/car.png'),
    ('merged today', 'items/tank.png'),
    ('folded', 'items/rocket.png'),
]

def stub_routes(page, fixture=None):
    # Network-level route interception, registered BEFORE navigation, so it
    # cannot race the page's own on-load fetch the way a post-hoc
    # window.fetch monkeypatch can (this app also has a real, working
    # /api/pipeline in this sandbox - a real background fetch racing in
    # after a same-tick fetch stub was installed was observed to
    # occasionally overwrite the fixture render mid-test).
    payload = fixture if fixture is not None else pipeline_fixture
    page.route('**/api/pipeline*', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(payload)))
    page.route('**/api/agents*', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(agents_fixture)))


def render(p, fixture, reduced_motion=False):
    """Fresh browser context, navigated and refreshed against `fixture`."""
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    if reduced_motion:
        page.emulate_media(reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    stub_routes(page, fixture)
    page.goto('http://127.0.0.1:5099/', wait_until='domcontentloaded')
    page.evaluate('async()=>{await refreshShipFlow();}')
    page.wait_for_timeout(1000)
    assert not errors, errors
    return browser, page


def sprite_bg_url(page, square):
    return page.locator('[data-square="' + square + '"] .ship-sprite').evaluate(
        "el => getComputedStyle(el).backgroundImage")


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
    # Belts + items (order 11 #2, order 10/17): every one of the 14 sequential
    # handoffs carries a real belt with at least one item; the deliberately-
    # stalled ci-green arrow's belt is packed/backed-up.
    assert page.locator('.ship-belt').count() == 14
    assert page.locator('.ship-belt-item').count() > 0
    assert page.locator('.ship-belt-backed-up').count() == 1
    # Round 2 defect 4: belts must read as belts, not connector widgets - the
    # brief's >=48px floor, with real margin above it.
    belt_width = page.eval_on_selector('.ship-belt:not(.ship-belt-vertical)', 'el => el.getBoundingClientRect().width')
    assert belt_width >= 64, belt_width
    # Round 2 defect 5: row 3 is no longer a lone square.
    assert page.locator('.ship-row-3 .ship-stage').count() == 5

    # Order 17 #1 "THE REAL ITEM CHAIN": the underlying mapping (not the
    # rendered DOM, since gate_verdicts/resolved legitimately render zero
    # item slots in THIS fixture - see pipeline_fixture_full_chain below for
    # the full-render proof) covers all 14 arrows, in Ben's own flow order,
    # each with its own named item - not a second arrow silently reusing a
    # neighbour's icon.
    live_chain = json.loads(page.evaluate('JSON.stringify(SHIP_ARROW_ITEM)'))
    assert list(live_chain.items()) == EXPECTED_CHAIN, (list(live_chain.items()), EXPECTED_CHAIN)

    # Order 17 #2 "no boxes around the belts": neither border nor outline on
    # any belt, including the backed-up (jam) one, which still reads red via
    # a glow only.
    for i in range(page.locator('.ship-belt').count()):
        b = page.locator('.ship-belt').nth(i)
        border = b.evaluate("el => getComputedStyle(el).borderStyle")
        outline = b.evaluate("el => getComputedStyle(el).outlineStyle")
        assert border == 'none', ('belt has a border', i, border)
        assert outline == 'none', ('belt has an outline', i, outline)

    # Order 17 #4 "an inserter on BOTH sides of every belt": exactly two
    # `.ship-inserter` children per arrow - loading and unloading - never one,
    # never a stray third.
    for square, _ in EXPECTED_CHAIN:
        n = page.locator('.ship-arrow[data-square-left="' + square + '"] > .ship-inserter').count()
        assert n == 2, (square, 'expected 2 inserters, found', n)

    # Order 17 #3 "belts run vertically": the two row-turn handoffs are
    # taller than they are wide; every other belt stays horizontal (wider
    # than tall).
    assert page.locator('.ship-arrow-vertical').count() == 2
    for square in ('review routed', 'resolved'):
        b = page.locator('.ship-arrow[data-square-left="' + square + '"] .ship-belt').bounding_box()
        assert b['height'] > b['width'], (square, b)
    for square, _ in EXPECTED_CHAIN:
        if square in ('review routed', 'resolved'):
            continue
        b = page.locator('.ship-arrow[data-square-left="' + square + '"] .ship-belt').bounding_box()
        assert b['width'] > b['height'], (square, b)

    # Round 2 (Elrond review, PR #35, defect 1): no stage may ever render a
    # bare '?' - it reads as indistinguishable from the no-data wreckage state
    # the sprite/remnant already carries. Every one of the 15 stages gets a
    # real sprite or the documented remnant; the count, sub-lines, and rate
    # panels render nothing rather than a glyph when a value is unknown.
    strip_text = strip.inner_text()
    assert '?' not in strip_text, strip_text

    # Round 3 (Elrond review, PR #35): every one of the 15 stages renders
    # either a number or an explicit 'n/a' - never nothing. gate_verdicts
    # (null since PR #34 dropped statusCheckRollup) was rendering an empty
    # ship-num, a third state indistinguishable from a rendering bug rather
    # than the same "I cannot know this" status 'resolved' already shows.
    stages = page.locator('.ship-stage')
    assert stages.count() == 15
    for i in range(stages.count()):
        stage = stages.nth(i)
        square = stage.get_attribute('data-square')
        num_text = stage.locator('.ship-num').inner_text() if stage.locator('.ship-num').count() else ''
        assert num_text.strip() != '', 'stage "' + square + '" rendered no number and no n/a'

    # Round 2 on PR #36 (Elrond review): BUGS FOUND clipped to "UGS FOUND"
    # again in round 2's own screenshots, a regression from the order-16 belt
    # widening squeezing row 1's stages narrower than their own captions.
    # Checked every stage this time, not just bugs found - a caption's own
    # bounding box must stay fully inside #ship-flow's captured area (which
    # is exactly what a Locator screenshot crops to), on both edges.
    flow_box = strip.bounding_box()
    flow_left, flow_right = flow_box['x'], flow_box['x'] + flow_box['width']
    for i in range(stages.count()):
        stage = stages.nth(i)
        square = stage.get_attribute('data-square')
        cap_box = stage.locator('.ship-cap').bounding_box()
        assert cap_box['x'] >= flow_left, (square, 'clipped at left edge', cap_box, flow_left)
        assert cap_box['x'] + cap_box['width'] <= flow_right, (square, 'clipped at right edge', cap_box, flow_right)

    # Round 2 defect 3: the row-turn elbows must sit tucked against their
    # anchor square, not floating - within ~40px of it in both axes.
    for elbow_id, anchor_square in (('ship-elbow-1', 'review routed'), ('ship-elbow-2', 'resolved')):
        elbow_box = page.locator('#' + elbow_id).bounding_box()
        anchor_box = page.locator('[data-square="' + anchor_square + '"]').bounding_box()
        assert elbow_box and anchor_box, (elbow_id, anchor_square)
        assert abs(elbow_box['y'] - anchor_box['y'] - anchor_box['height']) < 40, (elbow_id, elbow_box, anchor_box)

    # Order 14 "the inserters must swing": a moving inserter's arm carries a
    # real animation, its duration is BOUND to the measured rate (two
    # different real rates -> two different durations, not a constant), and
    # its delay is staggered (two moving inserters -> two different delays,
    # never lockstep). An unmeasured arrow's arm never animates at all.
    # Order 17 #4 put TWO `.ship-inserter` elements on every arrow (loading
    # then unloading), so a bare `.inserter-arm` locator now matches two
    # elements and `.evaluate()` throws. `which` picks one by its DOM
    # position (0 = loading, upstream; 1 = unloading, downstream).
    def arm_style(square, which='load'):
        idx = 0 if which == 'load' else 1
        return page.locator(
            '.ship-arrow[data-square-left="' + square + '"] > .ship-inserter'
        ).nth(idx).locator('.inserter-arm').evaluate(
            "el => ({name: getComputedStyle(el).animationName, "
            "duration: getComputedStyle(el).animationDuration, "
            "delay: getComputedStyle(el).animationDelay})")
    issues_arm = arm_style('issues open')   # rate 6/h, loading inserter
    prsci_arm = arm_style('prs open')       # rate 3/h, loading inserter
    dispatched_arm = arm_style('dispatched')  # no formal rate instrument
    assert issues_arm['name'] == 'inserter-swing', issues_arm
    assert prsci_arm['name'] == 'inserter-swing', prsci_arm
    assert issues_arm['duration'] != prsci_arm['duration'], (issues_arm, prsci_arm)
    assert issues_arm['delay'] != prsci_arm['delay'], (issues_arm, prsci_arm)
    assert dispatched_arm['name'] == 'none', dispatched_arm
    # ci-green is the fixture's bottleneck (rate 0, backlog 5) - BOTH its
    # inserters (loading and unloading) must freeze at the pickup end, not
    # swing, even though the arrow IS measured. A single frozen arm on a
    # jammed belt is a weaker signal than two.
    for which in ('load', 'unload'):
        ci_arm = page.locator(
            '.ship-arrow[data-square-left="ci q/run"] > .ship-inserter'
        ).nth(0 if which == 'load' else 1).evaluate("el => el.className")
        assert 'backed-up' in ci_arm, (which, ci_arm)
        ci_arm_anim = arm_style('ci q/run', which=which)
        assert ci_arm_anim['name'] == 'none', (which, ci_arm_anim)

    # Order 17 #4 "staggered ... not mirror images of each other": the
    # loading and unloading inserters on the SAME belt both swing, but on
    # different phases - never lockstep, which is what a mirror-image pair
    # moving identically would look like frame to frame.
    for square in ('issues open', 'prs open'):
        load = arm_style(square, which='load')
        unload = arm_style(square, which='unload')
        assert load['name'] == 'inserter-swing', (square, load)
        assert unload['name'] == 'inserter-swing', (square, unload)
        assert load['delay'] != unload['delay'], (
            square, 'loading/unloading inserters are lockstep (mirror-synced)', load, unload)

    # Order 15 "BUGS FOUND is a biter": the main fixture's count (3) is the
    # "small" state - never the corpse (measured zero) or the remnant/dim
    # (unknown) art, which are proven separately below with their own fixtures.
    assert 'small-biter.png' in sprite_bg_url(page, 'bugs found')
    assert 'corpse' not in sprite_bg_url(page, 'bugs found')

    # Three states side by side, proven never to look alike.
    page.locator('[data-square="bugs found"]').screenshot(path=str(OUT / 'state-idle.png'))
    page.locator('[data-square="ci q/run"]').screenshot(path=str(OUT / 'state-stalled.png'))

    # Greyscale fallback - the backed-up belt must still read as packed/dense.
    page.evaluate("document.documentElement.style.filter='grayscale(100%)'")
    page.wait_for_timeout(150)
    strip.screenshot(path=str(OUT / 'strip-1440-greyscale.png'))
    page.evaluate("document.documentElement.style.filter=''")

    # Order 16: a legibility check while we have this render up - the count
    # numerals must still be there and non-empty over the new ground texture.
    for square in ('bugs found', 'issues open', 'prs open', 'ci q/run'):
        num = page.locator('[data-square="' + square + '"] .ship-num').inner_text()
        assert num.strip() != '', square

    browser.close()

# Order 17 evidence: "a frame showing the full item chain readable across the
# strip - bug at the first handoff, rocket at the last." Uses
# pipeline_fixture_full_chain (gate_verdicts/resolved given a small real
# count) so every one of the 14 belts actually places a rendered item -
# the main fixture's two permanently-null arrows legitimately render empty
# belts there, which would leave two gaps in this proof.
with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture_full_chain)
    for square, expected_item in EXPECTED_CHAIN:
        belt = page.locator('.ship-arrow[data-square-left="' + square + '"] .ship-belt-item-img').first
        url = belt.evaluate("el => getComputedStyle(el).backgroundImage")
        assert expected_item in url, (square, expected_item, url)
    page.locator('#ship-flow').screenshot(path=str(OUT / 'strip-1440-item-chain.png'))
    browser.close()

# Separate pass for the no-data/remnant-wreckage state: prs_open unavailable
# THIS refresh (pipeline_fixture_no_data) is a different, real condition from
# gate_verdicts/resolved's permanent n/a, and needs its own proof - kept out
# of the main fixture above so it can't collide with the "never empty"
# assertion (a transient per-refresh miss legitimately still renders no
# number, only the sprite's remnant art carries that signal).
with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture_no_data)
    assert page.locator('[data-square="prs open"] .ship-sprite.remnant').count() == 1
    page.locator('[data-square="prs open"]').screenshot(path=str(OUT / 'state-no-data.png'))
    browser.close()

# Order 15: the three biter states, and the assertion order 15's evidence
# section explicitly asks for - corpse (measured zero) and the unknown/dim
# fallback are NOT the same asset, so "no bugs" and "nobody knows" can never
# look alike (the whole point of this dashboard, per the brief).
with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture_bugs_corpse)
    corpse_url = sprite_bg_url(page, 'bugs found')
    assert 'small-biter-corpse.png' in corpse_url, corpse_url
    page.locator('[data-square="bugs found"]').screenshot(path=str(OUT / 'biter-corpse.png'))
    browser.close()

with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture_bugs_medium)
    medium_url = sprite_bg_url(page, 'bugs found')
    assert 'medium-biter.png' in medium_url, medium_url
    page.locator('[data-square="bugs found"]').screenshot(path=str(OUT / 'biter-medium.png'))
    browser.close()

with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture_bugs_unknown)
    unknown_url = sprite_bg_url(page, 'bugs found')
    assert 'corpse' not in unknown_url, unknown_url
    assert corpse_url != unknown_url, (corpse_url, unknown_url)
    # Round 3's rule applies here too: unknown renders 'n/a', never empty.
    num_text = page.locator('[data-square="bugs found"] .ship-num').inner_text()
    assert num_text.strip() == 'n/a', num_text
    page.locator('[data-square="bugs found"]').screenshot(path=str(OUT / 'biter-unknown.png'))
    browser.close()

# Order 14 evidence: at least three frames at different points in the swing,
# proving arms actually moved (not a single still) AND that two different
# inserters are staggered (not swinging in lockstep). Real wall-clock waits
# between screenshots - the CSS animation runs on the browser's own clock,
# independent of these waits, so each capture is a genuine later point in
# the cycle, not a re-render of the same frame.
with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture)

    # Order 17 #4 put two `.ship-inserter` per arrow (loading=0, unloading=1).
    # `which` lets this proof track a specific one across frames.
    def arm_transform(sq, which=0):
        return page.locator(
            '.ship-arrow[data-square-left="' + sq + '"] > .ship-inserter'
        ).nth(which).locator('.inserter-arm').evaluate("el => getComputedStyle(el).transform")

    def transforms():
        return {
            (sq, which): arm_transform(sq, which)
            for sq in ('issues open', 'prs open', 'approved')
            for which in (0, 1)
        }

    frames = []
    for i in range(3):
        page.locator('#ship-flow').screenshot(path=str(OUT / ('swing-frame-' + str(i + 1) + '.png')))
        frames.append(transforms())
        if i < 2:
            page.wait_for_timeout(650)

    # Each moving inserter's own transform changed across the three frames -
    # it swung, this was not a static screenshot repeated three times. This
    # now covers BOTH inserters (loading and unloading) on each belt.
    for sq in ('issues open', 'prs open', 'approved'):
        for which in (0, 1):
            values = {f[(sq, which)] for f in frames}
            assert len(values) > 1, (sq, which, frames)
    # At any single frame, two inserters with different measured rates are at
    # DIFFERENT points of the arc - staggered, not lockstep.
    assert frames[0][('issues open', 0)] != frames[0][('prs open', 0)], frames[0]
    # Order 17 #4 "not mirror images of each other": on the SAME belt, the
    # loading and unloading inserters must ALSO be at different arc positions
    # at a given instant - two arms moving in lockstep would read as one
    # mirrored motion, not two independent handoffs.
    for sq in ('issues open', 'prs open', 'approved'):
        assert frames[0][(sq, 0)] != frames[0][(sq, 1)], (sq, frames[0])
    browser.close()

# Reduced-motion pass, in a fresh context (emulate_media must be set before
# navigation to take effect on first paint).
with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture, reduced_motion=True)
    assert page.eval_on_selector('.ship-belt-track', "el => getComputedStyle(el).animationName") == 'none'
    assert page.eval_on_selector('.ship-arrow.bottleneck', "el => getComputedStyle(el).animationName") == 'none'
    # Order 14: extend reduced-motion coverage to the swinging arm too.
    moving_arms = page.locator('.ship-inserter.moving .inserter-arm')
    assert moving_arms.count() > 0
    for i in range(moving_arms.count()):
        name = moving_arms.nth(i).evaluate("el => getComputedStyle(el).animationName")
        assert name == 'none', name
    page.locator('#ship-flow').screenshot(path=str(OUT / 'strip-1440-reduced-motion.png'))
    browser.close()

print('OK', OUT)
