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
    # Ben's row-3 closed-issues list: one issue with both legs measured, one
    # with neither (a lane never launched for it) - the second must render
    # n/a twice, never a guess.
    closed_issue_timings=[
        {'number': 7265, 'title': 'Capture memory button icon', 'bug_to_dispatch_min': 148, 'dispatch_to_deploy_min': 311},
        {'number': 7301, 'title': 'Prospect list sort order', 'bug_to_dispatch_min': None, 'dispatch_to_deploy_min': None},
    ],
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
# Ben's queue model: a belt shows the jobs waiting to enter the next stage
# (arrow.backlog), so the merged-deploy arrow's measured-zero queue in the
# main fixture correctly renders an EMPTY belt there; this fixture gives it
# one waiting job so the full chain (bug -> rocket) renders end to end.
pipeline_fixture_full_chain = dict(pipeline_fixture, gate_verdicts=3, resolved=2,
    arrows=[dict(a, backlog=(1 if a['key'] == 'merged-deploy' else a['backlog'])) for a in pipeline_fixture['arrows']])

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
    # Ben's 10:08 AM rig (2026-09-12): gate verdicts SPLITS - one belt left
    # to approved, one straight down to conflicted. Same source machine,
    # same item on both (a fork of one output, not a 15th item).
    ('gate verdicts', 'items/speed-module.png'),
    ('gate verdicts conflicted', 'items/speed-module.png'),
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
    # Round 2 defect 4 / Ben's own built reference (all 14 belts now run
    # vertical, per order 17 #3 below): belts must read as belts, not
    # connector widgets - the brief's >=48px floor, applied to the belt's
    # long axis (height, since every belt is now taller than wide).
    belt_height = page.eval_on_selector('.ship-belt-vertical', 'el => el.getBoundingClientRect().height')
    assert belt_height >= 48, belt_height
    # Ben's 10:08 AM rig (2026-09-12): conflicted and resolved drop to a
    # conflict loop on row 3, which frees room on row 2 for DEPLOYED -
    # 7 / 6 / 2, with DEPLOYED as row 2's left end.
    assert page.locator('.ship-row-1 .ship-stage').count() == 7
    assert page.locator('.ship-row-2 .ship-stage').count() == 6
    assert page.locator('.ship-row-3 .ship-stage').count() == 2
    assert page.locator('.ship-row-2 [data-square="last deploy"]').count() == 1
    assert [e.get_attribute('data-square') for e in page.locator('.ship-row-3 .ship-stage').all()] == ['conflicted', 'resolved']

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
    # Ben's rig: resolved is the one exception - it has NO belt of its own,
    # just a loading inserter placing onto the shared approved up-belt
    # (#ship-elbow-4), so it carries exactly one inserter and no unloader.
    for square, _ in EXPECTED_CHAIN:
        n = page.locator('.ship-arrow[data-square-left="' + square + '"] > .ship-inserter').count()
        if square == 'resolved':
            assert n == 1, (square, 'the feeder has exactly one (loading) inserter, found', n)
            assert page.locator('.ship-arrow[data-square-left="resolved"] .ship-belt').count() == 0
            assert page.locator('#ship-elbow-4 > .ship-arrow[data-square-left="resolved"]').count() == 1, 'resolved feeder must be nested in the shared belt'
            continue
        assert n == 2, (square, 'expected 2 inserters, found', n)

    # Order 17 #3 "belts run vertically": Ben's own built reference (a
    # vertical belt bridging two side-by-side buildings, both inserters
    # bending toward it) showed this doesn't require breaking the
    # boustrophedon row layout at all - only the belt WITHIN each arrow slot
    # needs to run top to bottom. All 14 belts are vertical (taller than
    # wide): the 2 row-turn handoffs via the absolute-positioned
    # `.ship-arrow-vertical` (bridging between rows), the other 12 via the
    # in-row, normal-flow `.ship-arrow-vbelt` (still between two
    # horizontally-adjacent stages in the same row).
    # Ben's rig: three tall column belts now (in review -> gate verdicts,
    # gate verdicts -> conflicted straight down the same column, and the
    # shared resolved/approved -> in line up-belt) plus 11 in-row belts
    # (6 on row 1, 4 on row 2, 1 on row 3) = the same 14.
    assert page.locator('.ship-arrow-vertical').count() == 3
    assert page.locator('.ship-arrow-vbelt').count() == 11
    for square, _ in EXPECTED_CHAIN:
        if square == 'resolved':
            continue
        b = page.locator('.ship-arrow[data-square-left="' + square + '"] .ship-belt').bounding_box()
        assert b['height'] > b['width'], (square, b)

    # Ben: chevrons alternate along a row. His 10:08 AM rig, read belt by
    # belt at native resolution: row 1 down/up/down/up/down/up; row 2 (from
    # gate verdicts leftward) down, UP (the shared belt - which is what lets
    # resolved ride it back up), down, up, down; row 3's one belt down.
    def flows_up(square):
        return 'ship-belt-vertical-up' in page.locator('.ship-arrow[data-square-left="' + square + '"] .ship-belt').get_attribute('class')
    row1 = ['bugs found', 'issues open', 'dispatched', 'prs open', 'ci q/run', 'review routed']
    row2 = ['gate verdicts', 'approved', 'in line', 'merged today', 'folded']
    row3 = ['conflicted']
    assert [flows_up(sq) for sq in row1] == [False, True, False, True, False, True], [(sq, flows_up(sq)) for sq in row1]
    assert [flows_up(sq) for sq in row2] == [False, True, False, True, False], [(sq, flows_up(sq)) for sq in row2]
    assert [flows_up(sq) for sq in row3] == [False], [(sq, flows_up(sq)) for sq in row3]
    # Ben: "the inserters are placing on the start and removing at the end"
    # - the loading inserter (DOM-first) sits on the upstream stage's side of
    # the belt: left of it in row 1 (flows left-to-right), RIGHT of it in the
    # row-reversed row 2.
    def load_side(square):
        arrow = page.locator('.ship-arrow[data-square-left="' + square + '"]')
        load = arrow.locator('> .ship-inserter').nth(0).bounding_box()
        belt = arrow.locator('.ship-belt').bounding_box()
        return 'right' if load['x'] > belt['x'] else 'left'
    for sq in row1:
        assert load_side(sq) == 'left', (sq, 'row 1 loading inserter must be left of the belt')
    for sq in row2 + row3:
        assert load_side(sq) == 'right', (sq, 'row 2/3 loading inserter must be right of the belt')
    # ...and on the shared belt the resolved feeder loads from the right too,
    # at the belt's START (its bottom - it flows up), while the unloader into
    # in line is on the left at the belt's END (its top).
    shared_belt = page.locator('#ship-elbow-4 .ship-belt').bounding_box()
    feeder = page.locator('#ship-elbow-4 .ship-arrow-feeder .ship-inserter').bounding_box()
    unload = page.locator('#ship-elbow-4 > .ship-inserter').nth(1).bounding_box()
    assert feeder['x'] > shared_belt['x'] and unload['x'] < shared_belt['x'], (feeder, unload, shared_belt)
    assert abs((feeder['y'] + feeder['height']) - (shared_belt['y'] + shared_belt['height'])) < 6, ('feeder not at the belt start (bottom)', feeder, shared_belt)
    assert abs(unload['y'] - shared_belt['y']) < 6, ('unloader not at the belt end (top)', unload, shared_belt)

    # Row-end belts carry work DOWN to the next row - except the shared
    # belt, which is the one that carries resolved work back UP.
    for sq in ('in review', 'gate verdicts conflicted'):
        assert not flows_up(sq), (sq, 'row-end belt must flow down')
    assert flows_up('approved'), 'the shared resolved/approved belt must flow up'

    # Ben: "I don't want to lose the spark lines" - the five history
    # sparklines plus the issues-rate panel still render, one per stage that
    # had one before the Factorio rebuild, each at least 100px wide and
    # 20px tall (not squeezed to the sprite's width).
    for square in ('prs open', 'ci q/run', 'in line', 'merged today', 'last deploy'):
        sb = page.locator('[data-square="' + square + '"] .ship-history-spark').first.bounding_box()
        assert sb and sb['width'] >= 100 and sb['height'] >= 20, (square, 'sparkline missing or squeezed', sb)
    rb = page.locator('[data-square="issues open"] .ship-issues-rate').bounding_box()
    assert rb and rb['width'] >= 100, ('issues open', 'rate sparkline missing or squeezed', rb)

    # Ben's closed-issues list on row 3: hh:mm for each measured leg, n/a for
    # an unmeasured one - and it sits in row 3's open space, which is now on
    # the LEFT of the conflict loop, clear of the shared belt's column.
    closed = page.locator('.ship-row-3 .ship-closed')
    assert closed.count() == 1
    closed_text = closed.inner_text()
    assert '#7265' in closed_text and '02:28' in closed_text and '05:11' in closed_text, closed_text
    assert '#7301' in closed_text and closed_text.count('n/a') >= 2, closed_text
    closed_box = closed.bounding_box()
    shared_box = page.locator('#ship-elbow-4').bounding_box()
    assert closed_box['x'] + closed_box['width'] <= shared_box['x'], ('closed list overlaps the shared belt column', closed_box, shared_box)

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

    # Ben's reference: each row-end belt is a tall column beside the row's
    # last stage that runs all the way down to beside the next row's first
    # stage - its top is at the from-stage's top and its bottom reaches the
    # to-stage's bottom, not a short stub floating in the gap between rows.
    # Ben's 10:08 AM rig: the right-hand column runs from IN REVIEW down
    # past GATE VERDICTS to CONFLICTED as TWO belts at the same x with a
    # visible split beside the gate (Ben, 10:35 AM: "should actually have a
    # split in it - easy to miss"): #ship-elbow-1 ends at the unloader into
    # the gate, a tile of bare ground, then #ship-elbow-3 starts at the
    # loader out of it. Gap centred on the gate's machine.
    def sprite_box(sq):
        return page.locator('[data-square="' + sq + '"] .ship-sprite-wrap').bounding_box()
    gate_box = sprite_box('gate verdicts')
    gate_mid = gate_box['y'] + gate_box['height'] / 2
    split = page.evaluate('SHIP_COLUMN_SPLIT_PX')
    assert 16 <= split <= 32, ('the column split must be a visible gap about one belt-width wide', split)
    for elbow_id, from_sq, to_sq, top_at, bottom_at in (
            ('ship-elbow-1', 'in review', 'gate verdicts', sprite_box('in review')['y'], gate_mid - split / 2),
            ('ship-elbow-3', 'gate verdicts', 'conflicted', gate_mid + split / 2, sprite_box('conflicted')['y'] + sprite_box('conflicted')['height'])):
        elbow_box = page.locator('#' + elbow_id).bounding_box()
        # Ben: the top/bottom inserters must sit level with the two MACHINES
        # (sprites), not the stage boxes' outer edges - captions and
        # sub-lines below a sprite were dragging the unloading inserter too
        # low and a tall neighbour was pushing the loading one too high.
        from_box = sprite_box(from_sq)
        to_box = sprite_box(to_sq)
        assert elbow_box and from_box and to_box, (elbow_id, from_sq, to_sq)
        assert abs(elbow_box['y'] - top_at) < 6, (elbow_id, 'top misplaced', elbow_box, top_at)
        assert abs((elbow_box['y'] + elbow_box['height']) - bottom_at) < 6, (elbow_id, 'bottom misplaced', elbow_box, bottom_at)
        top_ins = page.locator('#' + elbow_id + ' .ship-inserter').nth(0).bounding_box()
        bot_ins = page.locator('#' + elbow_id + ' .ship-inserter').nth(1).bounding_box()
        assert from_box['y'] <= top_ins['y'] + top_ins['height'] / 2 <= from_box['y'] + from_box['height'], (elbow_id, 'loading inserter not level with from-sprite', top_ins, from_box)
        assert to_box['y'] <= bot_ins['y'] + bot_ins['height'] / 2 <= to_box['y'] + to_box['height'], (elbow_id, 'unloading inserter not level with to-sprite', bot_ins, to_box)
        # Ben: "we lost the transport belt going from row 2 to 3" - the whole
        # column must be inside the strip's frame, never clipped off an edge.
        assert elbow_box['x'] >= flow_left and elbow_box['x'] + elbow_box['width'] <= flow_right, (elbow_id, 'row-end belt outside the frame', elbow_box, flow_left, flow_right)
        belt_box = page.locator('#' + elbow_id + ' .ship-belt').bounding_box()
        assert belt_box['x'] >= flow_left and belt_box['x'] + belt_box['width'] <= flow_right, (elbow_id, 'row-end belt column clipped', belt_box, flow_left, flow_right)
    e1 = page.locator('#ship-elbow-1').bounding_box(); e3 = page.locator('#ship-elbow-3').bounding_box()
    assert abs(e1['x'] - e3['x']) < 1, ('the two column belts must share one x - one straight column', e1, e3)
    gap = e3['y'] - (e1['y'] + e1['height'])
    assert 16 <= gap <= 32, ('the two column belts must show a visible split beside the gate, not butt end to end', gap, e1, e3)
    # ...and the gap is bare ground: the belts themselves (not just the
    # arrow boxes) stop short of each other by the same amount.
    b1 = page.locator('#ship-elbow-1 .ship-belt').bounding_box(); b3 = page.locator('#ship-elbow-3 .ship-belt').bounding_box()
    assert b3['y'] - (b1['y'] + b1['height']) >= 16, ('belt tracks must not bridge the split', b1, b3)

    # The shared up-belt (#ship-elbow-4): top level with IN LINE's machine
    # (its unloader), bottom at RESOLVED's machine bottom (the feeder, at the
    # belt's start), approved's loader level with APPROVED's machine.
    e4 = page.locator('#ship-elbow-4').bounding_box()
    inline_box, approved_box, resolved_box = sprite_box('in line'), sprite_box('approved'), sprite_box('resolved')
    # Ben (10:35 AM): the resolved feeder is CENTRED on resolved's chest,
    # and the belt starts right there (it ends where the feeder ends).
    feeder_ins0 = page.locator('#ship-elbow-4 .ship-arrow-feeder .ship-inserter').bounding_box()
    assert abs((feeder_ins0['y'] + feeder_ins0['height'] / 2) - (resolved_box['y'] + resolved_box['height'] / 2)) < 3, ('resolved feeder not centred on resolved', feeder_ins0, resolved_box)
    assert abs((e4['y'] + e4['height']) - (feeder_ins0['y'] + feeder_ins0['height'])) < 3, ('shared belt does not end at the feeder', e4, feeder_ins0)
    load_ins = page.locator('#ship-elbow-4 > .ship-inserter').nth(0).bounding_box()
    unload_ins = page.locator('#ship-elbow-4 > .ship-inserter').nth(1).bounding_box()
    feeder_ins = page.locator('#ship-elbow-4 .ship-arrow-feeder .ship-inserter').bounding_box()
    def level(ins, box, what):
        c = ins['y'] + ins['height'] / 2
        assert box['y'] <= c <= box['y'] + box['height'], (what, 'inserter not level with its machine', ins, box)
    level(load_ins, approved_box, 'approved loader')
    level(unload_ins, inline_box, 'in line unloader')
    level(feeder_ins, resolved_box, 'resolved feeder')
    assert e4['x'] >= flow_left and e4['x'] + e4['width'] <= flow_right, ('shared belt outside the frame', e4)
    # It runs in the column both rows reserve for it - between approved and
    # in line on row 2, and beside resolved on row 3 - overlapping no stage.
    for sq in ('approved', 'in line', 'resolved', 'conflicted', 'gate verdicts'):
        sb = page.locator('[data-square="' + sq + '"]').bounding_box()
        assert sb['x'] + sb['width'] <= e4['x'] + 1 or sb['x'] >= e4['x'] + e4['width'] - 1, ('shared belt overlaps', sq, sb, e4)
    assert approved_box['x'] > e4['x'] and inline_box['x'] < e4['x'], ('shared belt must sit between in line (left) and approved (right)', inline_box, e4, approved_box)

    # Ben's rig: CONFLICTED sits directly under GATE VERDICTS and RESOLVED
    # directly under APPROVED - sprite centres line up column for column.
    for above, below in (('gate verdicts', 'conflicted'), ('approved', 'resolved')):
        a, b = sprite_box(above), sprite_box(below)
        assert abs((a['x'] + a['width'] / 2) - (b['x'] + b['width'] / 2)) < 2, (below, 'not under', above, a, b)

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
            "el => { const a = el.getAnimations()[0]; if (!a) return {name: 'none', duration: '0s', phase: null};"
            " const t = a.effect.getTiming(); return {name: a.id, duration: (t.duration / 1000) + 's', phase: Math.round(((a.currentTime % t.duration) / t.duration) * 100) / 100, start: Number(el.dataset.swingStart)}; }")
    issues_arm = arm_style('issues open')   # rate 6/h, loading inserter
    prsci_arm = arm_style('prs open')       # rate 3/h, loading inserter
    dispatched_arm = arm_style('dispatched')  # no formal rate instrument
    assert issues_arm['name'] == 'inserter-swing', issues_arm
    assert prsci_arm['name'] == 'inserter-swing', prsci_arm
    assert issues_arm['duration'] != prsci_arm['duration'], (issues_arm, prsci_arm)
    # Two belts with different periods (4s, 8s) drift in and out of step by
    # nature, so their stagger is the seeded start offset, not a live phase.
    assert issues_arm['start'] != prsci_arm['start'], ('two belts seeded in lockstep', issues_arm, prsci_arm)
    assert dispatched_arm['name'] == 'none', dispatched_arm
    # Ben: an inserter only swings when there is a job on its belt to move.
    # merged-deploy has a measured rate (1/h) but an empty queue (backlog 0)
    # in this fixture - both its inserters must be idle, not swinging.
    for which in ('load', 'unload'):
        empty_arm = arm_style('merged today', which=which)
        assert empty_arm['name'] == 'none', ('merged today', which, 'swinging with nothing on the belt', empty_arm)
    # Ben asked what "remnant" means: not time-based - the wreck sprite only
    # when the handoff's count is n/a. 'dispatched' has a plain count (2) and
    # no rate instrument, so its inserters are intact and idle, not wrecked;
    # 'resolved' (count n/a in this fixture) is the one that wrecks.
    dispatched_cls = page.locator('.ship-arrow[data-square-left="dispatched"] > .ship-inserter').nth(0).get_attribute('class')
    assert 'remnant' not in dispatched_cls and 'idle' in dispatched_cls, dispatched_cls
    resolved_cls = page.locator('.ship-arrow[data-square-left="resolved"] > .ship-inserter').nth(0).get_attribute('class')
    assert 'remnant' in resolved_cls, resolved_cls
    # Ben: "swap out all the long handled inserters with fast inserters" -
    # every platform is the fast (blue) inserter sheet; nothing long-handed
    # is served any more.
    plat = page.locator('.ship-inserter .inserter-platform').first.evaluate("el => getComputedStyle(el).backgroundImage")
    assert 'fast-inserter-platform' in plat, plat
    assert page.locator('.ship-inserter [style*="long-handed"]').count() == 0
    # Ben: the swing is the full 180 - pickup one side of the base, drop on
    # the other. Read the keyframe rule itself: its two extremes are 180deg
    # apart.
    # Real numbers (Ben, 2:30 PM): the swing is a Web Animation built from
    # the game's fast-inserter prototype - 180deg each way at rotation_speed
    # 0.04 turns/tick (0.2083s), reaching 1.0 tile at pickup and 1.2 at the
    # drop, then WAITING at the pickup side for the rest of the period.
    kf = page.locator('.ship-arrow[data-square-left="issues open"] > .ship-inserter').nth(0).locator('.inserter-arm').evaluate(
        "el => { const a = el.getAnimations()[0]; return {frames: a.effect.getKeyframes().map(k => [k.offset, k.transform]), duration: a.effect.getTiming().duration}; }")
    import re
    angles = [float(re.search(r'rotate\((-?[\d.]+)deg\)', f[1]).group(1)) for f in kf['frames']]
    scales = [float(re.search(r'scaleY\(([\d.]+)\)', f[1]).group(1)) for f in kf['frames']]
    assert abs(angles[1] - angles[0]) == 180, ('full 180 swing', kf)
    assert abs(scales[0] - 24 / 31) < 0.01 and abs(scales[1] - 1.2 * 24 / 31) < 0.01, ('hand must reach 1.0 tile at pickup and 1.2 at the drop', scales)
    swing_s = 0.5 / 0.04 / 60
    assert abs(kf['frames'][1][0] * kf['duration'] / 1000 - swing_s) < 0.002, ('drop must land 0.2083s into the period (rotation_speed 0.04)', kf)
    assert abs(kf['frames'][3][0] * kf['duration'] / 1000 - 2 * swing_s) < 0.004, ('back at pickup after a second 0.2083s, then hold', kf)
    assert kf['frames'][-1][0] == 1 and abs(angles[-1] - angles[0]) < 0.01, ('arm must wait at the pickup side for the rest of the period', kf)
    assert kf['duration'] == 4000, ('issues open at 6/h swings once every 4s', kf['duration'])
    # Ben (10:45 AM 2026-09-12): the work product rides in the hand. A moving
    # inserter's arm carries the arrow's own item, on the SAME swing (same
    # duration and delay as the arm), shown for the carrying half and gone
    # at the drop; an idle inserter carries nothing.
    for square, item in (('issues open', 'items/lab.png'), ('prs open', 'items/copper-plate.png')):
        for which in (0, 1):
            arm = page.locator('.ship-arrow[data-square-left="' + square + '"] > .ship-inserter').nth(which).locator('.inserter-arm')
            held = arm.locator('.inserter-item')
            assert held.count() == 1, (square, which, 'moving inserter carries no item')
            st = held.evaluate("el => { const a = el.getAnimations()[0]; return a ? {img: getComputedStyle(el).backgroundImage, name: a.id, duration: a.effect.getTiming().duration, now: a.currentTime} : {img: getComputedStyle(el).backgroundImage, name: 'none', duration: 0, now: null}; }")
            armst = arm.evaluate("el => { const a = el.getAnimations()[0]; return {duration: a.effect.getTiming().duration, now: a.currentTime}; }")
            assert item in st['img'], (square, which, st)
            assert st['name'] == 'inserter-carry' and st['duration'] == armst['duration'] and abs(st['now'] - armst['now']) < 40, (square, which, 'item not on the arm\'s own swing', st, armst)
    assert page.locator('.ship-inserter.idle .inserter-item').count() == 0, 'an idle inserter must hold nothing'
    assert page.locator('.ship-arrow-feeder .inserter-item').count() == 0, 'resolved feeder is idle in this fixture (no rate) - holds nothing'
    carry = page.locator('.ship-arrow[data-square-left="issues open"] > .ship-inserter').nth(0).locator('.inserter-item').evaluate(
        "el => el.getAnimations()[0].effect.getKeyframes().map(k => [k.offset, k.opacity])")
    assert carry[0] == [0, '1'] and carry[-1] == [1, '0'] and abs(carry[1][0] - swing_s / 4) < 0.002, ('item must be held to the drop (0.2083s in) and gone after', carry)

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
        assert abs(load['phase'] - unload['phase']) > 0.02, (
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
        # Ben's rig: resolved's item rides the SHARED belt (#ship-elbow-4),
        # queued behind approved's own - so that belt carries both items.
        sel = '#ship-elbow-4 .ship-belt-item-img' if square == 'resolved' else '.ship-arrow[data-square-left="' + square + '"] .ship-belt-item-img'
        urls = [e.evaluate("el => getComputedStyle(el).backgroundImage") for e in page.locator(sel).all()]
        assert any(expected_item in u for u in urls), (square, expected_item, urls)
    shared_urls = [e.evaluate("el => getComputedStyle(el).backgroundImage") for e in page.locator('#ship-elbow-4 .ship-belt-item-img').all()]
    # fixture: green-inline backlog 1 (processing unit), resolved 2 (speed module 3),
    # in that order from the downstream (top) end.
    assert [('processing-unit' in u, 'speed-module-3' in u) for u in shared_urls] == [(True, False), (False, True), (False, True)], shared_urls
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

# REAL SCALE (Ben, 2:30 PM CDT 2026-09-12) - one tile = 24px, every sprite
# at its Lua size: assembling machine 3x3 (72px footprint, 80x89 frame),
# steel chest 1x1 (24px), fast inserter 1x1 (39x30 platform, 14x31 hand),
# belt one tile wide, rocket silo 9x9 (216px); belts at 1.875 tiles/s;
# assembler gears at the game's 30 fps when working.
with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture)
    def wrap_box(sq):
        return page.locator('[data-square="' + sq + '"] .ship-sprite-wrap').bounding_box()
    assert wrap_box('dispatched')['width'] == 72 and wrap_box('dispatched')['height'] == 72, wrap_box('dispatched')
    assert wrap_box('issues open')['width'] == 24 and wrap_box('issues open')['height'] == 24, wrap_box('issues open')
    assert wrap_box('last deploy')['width'] == 216 and wrap_box('last deploy')['height'] == 216, wrap_box('last deploy')
    assert abs(wrap_box('dispatched')['width'] / wrap_box('issues open')['width'] - 3) < 0.01, 'an assembler is 3 chests wide'
    assert abs(wrap_box('last deploy')['width'] / wrap_box('issues open')['width'] - 9) < 0.01, 'the silo is 9 chests wide'
    plat = page.locator('.ship-inserter .inserter-platform').first.bounding_box()
    assert plat['width'] == 39 and plat['height'] == 30, plat
    hand = page.locator('.ship-inserter .inserter-arm').first.evaluate("el => [el.offsetWidth, el.offsetHeight]")
    assert hand == [14, 31], hand
    belt = page.locator('.ship-belt-vertical').first.bounding_box()
    assert belt['width'] == 24, belt
    # Belt tread: the game's own south/north frames, one tile per 24px, at belt speed.
    track = page.locator('.ship-arrow[data-square-left="bugs found"] .ship-belt-track').evaluate(
        "el => ({img: getComputedStyle(el).backgroundImage, size: getComputedStyle(el).backgroundSize, dur: getComputedStyle(el).animationDuration, name: getComputedStyle(el).animationName})")
    assert 'belt-tile-south-24' in track['img'] and track['size'] == '24px 24px', track
    assert abs(float(track['dur'].rstrip('s')) - 1 / (0.03125 * 60)) < 0.002 and track['name'] != 'none', ('belt must run at 0.03125 tiles/tick', track)
    up = page.locator('.ship-arrow[data-square-left="issues open"] .ship-belt-track').evaluate("el => getComputedStyle(el).backgroundImage")
    assert 'belt-tile-north-24' in up, up
    # Assembler gears: every machine with a job in it animates through the
    # 32-frame sheet at 30 fps (0.2667s across 8 columns, 1.0667s down 4 rows);
    # gate verdicts (n/a) and a machine at 0 stay still.
    working = page.locator('.ship-sprite.asm3.working')
    working_sq = [e.evaluate("el => el.closest('.ship-stage').dataset.square") for e in working.all()]
    assert sorted(working_sq) == sorted(['dispatched', 'ci q/run', 'in review', 'merged today', 'folded']), working_sq
    gv = page.locator('[data-square="gate verdicts"] .ship-sprite.asm3')
    assert gv.count() == 1 and 'working' not in gv.get_attribute('class'), 'a machine with no job must not spin'
    anim = working.first.evaluate("el => ({names: getComputedStyle(el).animationName, durs: getComputedStyle(el).animationDuration, size: getComputedStyle(el).backgroundSize, tf: getComputedStyle(el).animationTimingFunction})")
    assert anim['names'] == 'asm3-x, asm3-y' and anim['size'] == '640px 356px', anim
    d = [float(x.strip().rstrip('s')) for x in anim['durs'].split(',')]
    assert abs(d[0] - 8 / 30) < 0.002 and abs(d[1] - 32 / 30) < 0.002, ('32 frames at animation_speed 0.5 = 30 fps', d)
    assert 'steps(8' in anim['tf'] and 'steps(4' in anim['tf'], anim['tf']
    # ...and the frame actually advances: background-position changes over time.
    pos0 = working.first.evaluate("el => getComputedStyle(el).backgroundPosition")
    page.wait_for_timeout(120)
    pos1 = working.first.evaluate("el => getComputedStyle(el).backgroundPosition")
    assert pos0 != pos1, ('gears did not turn', pos0, pos1)
    # Grass: the rendered field, not a couple of tiles - a period of at least
    # 1536x768 (Ben: "looks like you just used a few").
    bg = page.evaluate("getComputedStyle(document.querySelector('.ship-flow-wrap')).backgroundSize")
    assert bg == '1536px 768px', bg
    browser.close()

# ONE SCREEN (Ben, 3:27 PM CDT 2026-09-12): "tighten everything up
# vertically - I'd like the full CI/CD to fit on one screen easily." His
# window is 1512x850 CSS px (3024x1964 retina, browser chrome off), the page
# header puts the strip's top at ~196. With a full 10-row closed-issues
# list the whole strip (legend, three rows, list, silo) must end well above
# the fold, and nothing may overlap: row 3 is pulled up beside the silo, so
# the closed list must stay clear of DEPLOYED and the loop clear of row 2.
pipeline_fixture_ten_closed = dict(pipeline_fixture, closed_issue_timings=[
    dict(pipeline_fixture['closed_issue_timings'][0], number=7000 + i, title='Closed issue number %d with a longish title to clip' % i) for i in range(10)])
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1512, 'height': 850})
    stub_routes(page, pipeline_fixture_ten_closed)
    page.goto('http://127.0.0.1:5099/', wait_until='domcontentloaded')
    page.evaluate('async()=>{await refreshShipFlow();}')
    page.wait_for_timeout(1200)
    flow = page.locator('#ship-flow').bounding_box()
    assert flow['y'] + flow['height'] <= 800, ('strip runs past a 850px window', flow)
    assert flow['height'] <= 620, ('strip taller than one screen allows', flow)
    def box(sel):
        # The union of a stage's CHILDREN, not its flex box - row 2's boxes
        # all stretch to the silo's height, which is empty ground below the
        # short stages, not content.
        return page.locator(sel).evaluate('''el => { let l = 1e9, t = 1e9, r = -1e9, b = -1e9;
            for (const c of el.children) { const q = c.getBoundingClientRect(); if (!q.width) continue; l = Math.min(l, q.left); t = Math.min(t, q.top); r = Math.max(r, q.right); b = Math.max(b, q.bottom); }
            if (l > r) { const q = el.getBoundingClientRect(); return {x: q.left, y: q.top, width: q.width, height: q.height}; }
            return {x: l, y: t, width: r - l, height: b - t}; }''')
    def overlap(a, b):
        return a['x'] < b['x'] + b['width'] and b['x'] < a['x'] + a['width'] and a['y'] < b['y'] + b['height'] and b['y'] < a['y'] + a['height']
    deployed = box('[data-square="last deploy"]'); closed = box('.ship-closed')
    assert not overlap(deployed, closed), ('closed list overlaps DEPLOYED', deployed, closed)
    for sq in ('conflicted', 'resolved'):
        for above in ('gate verdicts', 'approved', 'in line'):
            assert not overlap(box('[data-square="' + sq + '"]'), box('[data-square="' + above + '"]')), (sq, 'overlaps', above)
    legend = box('#ship-flow .ship-legend')
    # The legend lives in the panel's head row (above the ground, beside the
    # repo dropdown) so it costs the strip no height - and must not sit on
    # the dropdown or the refresh button.
    ground = page.locator('#ship-flow').evaluate("el => { const q = el.parentElement.getBoundingClientRect(); return {x: q.left, y: q.top, width: q.width, height: q.height}; }")
    assert legend['y'] + legend['height'] <= ground['y'] + 1, ('legend must sit above the ground, in the head row', legend, ground)
    for sel in ('#glance-strip select', '#glance-strip .panel-refresh-btn'):
        other = page.locator(sel).first.bounding_box()
        assert other and not overlap(legend, other), ('legend overlaps', sel, legend, other)
    # The closed list must clear every row-2 belt and inserter above it.
    for i in range(page.locator('.ship-row-2 .ship-arrow').count()):
        ab = page.locator('.ship-row-2 .ship-arrow').nth(i).bounding_box()
        assert not overlap(closed, ab), ('closed list overlaps a row-2 belt', closed, ab)
    assert not overlap(closed, box('#ship-elbow-4')), 'closed list overlaps the shared belt'
    page.screenshot(path=str(OUT / 'one-screen-1512x850.png'))
    browser.close()

# Order 14 evidence: at least three frames at different points in the swing,
# proving arms actually moved (not a single still) AND that two different
# inserters are staggered (not swinging in lockstep). Real wall-clock waits
# between screenshots - the CSS animation runs on the browser's own clock,
# independent of these waits, so each capture is a genuine later point in
# the cycle, not a re-render of the same frame.
with sync_playwright() as p:
    browser, page = render(p, pipeline_fixture)

    # Real timing (Ben, 2:30 PM): the swing is 0.2083s out and 0.2083s back,
    # then the arm WAITS at the pickup side for the rest of its period (4s at
    # 6/h) - so three wall-clock frames would mostly catch it waiting. The
    # frames are taken by seeking every swing/carry animation to the same
    # point of its own active time (0 = pickup, 104ms = mid-swing, 208ms =
    # the drop) and pausing there, so each screenshot is a real rendered
    # frame of the real animation at a known instant.
    def seek_all(ms):
        page.evaluate("ms => document.getAnimations().forEach(a => { if (a.id === 'inserter-swing' || a.id === 'inserter-carry') { a.pause(); a.currentTime = ms; } })", ms)
    def arm_transform(sq, which=0):
        return page.locator(
            '.ship-arrow[data-square-left="' + sq + '"] > .ship-inserter'
        ).nth(which).locator('.inserter-arm').evaluate("el => getComputedStyle(el).transform")
    def item_opacity(sq, which=0):
        return page.locator(
            '.ship-arrow[data-square-left="' + sq + '"] > .ship-inserter'
        ).nth(which).locator('.inserter-item').evaluate("el => getComputedStyle(el).opacity")
    def transforms():
        return {
            (sq, which): arm_transform(sq, which)
            for sq in ('issues open', 'prs open', 'approved')
            for which in (0, 1)
        }
    frames = []
    for i, ms in enumerate((0, 104, 208)):
        seek_all(ms)
        page.wait_for_timeout(60)
        page.locator('#ship-flow').screenshot(path=str(OUT / ('swing-frame-' + str(i + 1) + '.png')))
        frames.append(transforms())
    # Each moving inserter's arm is at three different positions at pickup,
    # mid-swing and drop - both inserters on every belt.
    for sq in ('issues open', 'prs open', 'approved'):
        for which in (0, 1):
            values = [f[(sq, which)] for f in frames]
            assert len(set(values)) == 3, (sq, which, values)
    # The item is in the closed hand through the swing and gone right after the drop.
    seek_all(104); assert item_opacity('issues open') == '1', 'item must be in hand mid-swing'
    seek_all(220); assert item_opacity('issues open') == '0', 'item must be dropped after 0.2083s'
    # (Stagger - loading vs unloading, belt vs belt - is proven above via
    # arm_style()'s phase, on the live clocks; seeking here put every arm on
    # one clock on purpose, so it is not re-checked after this pass.)
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
        n = moving_arms.nth(i).evaluate("el => getComputedStyle(el).animationName + '/' + el.getAnimations().length")
        assert n == 'none/0', n
    for i in range(page.locator('.ship-inserter.moving .inserter-item').count()):
        n = page.locator('.ship-inserter.moving .inserter-item').nth(i).evaluate("el => getComputedStyle(el).animationName + '/' + el.getAnimations().length")
        assert n == 'none/0', ('held item still animates under reduced motion', n)
    # ...and the assembler gears stop too.
    assert page.locator('.ship-sprite.asm3.working').count() > 0
    assert page.eval_on_selector('.ship-sprite.asm3.working', "el => getComputedStyle(el).animationName") == 'none'
    page.locator('#ship-flow').screenshot(path=str(OUT / 'strip-1440-reduced-motion.png'))
    browser.close()

print('OK', OUT)
