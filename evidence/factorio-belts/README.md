Evidence for LORE order 11 ("the sprite is the square") - the visual rebuild
of the 15-stage strip: card removal, real belts carrying the order-10 item
chain, and rate-driven animation. Captured against this worktree's own Flask
process (port 5099), not the live `queue-router.service` - that was never
touched or restarted.

**Round 2 (Elrond review, PR #35, 5 blocking defects - all fixed):**
1. No stage ever renders a bare `?` any more (ship-num, the issues-rate
   sparkline panel, the "oldest:" sub-line, and the deploy sub-line all
   render nothing, or a real word like "unknown"/"n/a", instead) - the sprite
   or its remnant is the only thing allowed to say "no data" now.
2. `BUGS FOUND` no longer clips at the left edge - a side effect of fix 5's
   row rebalance giving row 1 more width per stage.
3. The boustrophedon turn arrows (`#ship-elbow-1`/`#ship-elbow-2`) are now
   `position:absolute` and JS-synced (`positionShipElbows`) to sit tucked
   against the actual corner of the square they turn from, instead of
   floating in whatever gap a row's shortest column happened to leave.
4. Belts widened from 56px to 76px (brief's floor was 48px) with larger
   16px items - no longer reads as a connector widget.
5. Rows rebalanced from 7/7/1 to 6/4/5 - the two new row breaks fall exactly
   where the arrow was already decorative (no formal rate/backlog instrument),
   so no measured belt (issues-prs, prs-ci, ci-green, green-inline,
   inline-merged, merged-deploy) moved or was dropped.

`FOLDED` and `RESOLVED` were also flagged as "indistinct grey lumps" - checked
against the source PNGs (`static/factorio/roboport.png`,
`static/factorio/construction-robot.png`): both are the correct, intended
sprites per `SHIP_STAGE_META`, not a fallback. They are just inherently
grey/tan Factorio art at small size; left unchanged.

**Round 3 (Elrond review, PR #35, 1 blocker - fixed):** `GATE VERDICTS` was
rendering NO number at all (not even `?` after Round 2's fix removed that -
just nothing), while `RESOLVED` correctly rendered `n/a` in the same
screenshot. Both fields have carried the identical "no instrument for this,
ever" status since PR #34 dropped `statusCheckRollup` - `gate_verdicts` just
never got `resolved`'s null-to-`'n/a'` conversion at its call site. Fixed by
mirroring `resolved`'s exact pattern (same muted/dim treatment, same
`_na_reason` tooltip suffix). `strip-1440-full.png` now carries `resolved`
null too, specifically so both squares' matching `n/a` treatment is provable
in one screenshot, the same comparison the review made. Only that one call
site changed in `queue_router.py` - the other four Round 2 defects were
untouched, per the review's "do not change anything else".

`tests/dashboard-truth/factorio-belts-evidence.py` grows one more assertion:
every one of the 15 stages renders a number or an explicit `n/a`, never
nothing. Mutation-tested (reverted the gate_verdicts fix, the assertion
failed by name - `AssertionError: stage "gate verdicts" rendered no number
and no n/a` - then restored). The pre-existing no-data/remnant-wreckage
demo (`state-no-data.png`) moved to its own fixture
(`pipeline_fixture_no_data`) so a transient per-refresh miss (a real,
different condition from gate_verdicts/resolved's permanent `n/a`) can still
legitimately render no number without tripping the new blanket assertion.

- `strip-1440-full.png`: full strip at 1440px against a synthetic fixture with
  one deliberately measured-zero/backlogged arrow (`ci-green`), so a backed-up
  belt is visible alongside normal flowing ones.
- `strip-1440-LIVE-armbrain.png`: the same strip against this worktree's own
  server hitting **real** `armbrain-io/armbrain` GitHub data (`/api/pipeline`
  returned `available: true`, HTTP 200) - proves the rebuild survives real
  payload shapes, not just the fixture.
- `state-idle.png` / `state-stalled.png` / `state-no-data.png`: three squares
  from the same fixture render, side by side by name - idle (intact, fresh),
  stalled (intact, red "4h" age line, not wreckage), no-data (remnant
  wreckage sprite, no numeral at all) - proving the three states stay
  visually distinct, per the lore's explicit "must never look alike" rule.
- `strip-1440-greyscale.png`: `grayscale(100%)` filter applied - the backed-up
  belt is still legible from packing/density alone (shape/mass), not color.
- `strip-1440-reduced-motion.png`: rendered under
  `prefers-reduced-motion: reduce` (Playwright's `emulate_media`) - confirmed
  via computed style that `.ship-belt-track` and `.ship-arrow.bottleneck`
  both report `animationName: none` in this mode.

Run with:

```sh
python3 -c "import queue_router as q; q.init_db(); q.app.run(host='127.0.0.1', port=5099)" &
python3 tests/dashboard-truth/factorio-belts-evidence.py
```

(`init_db()` is required once per fresh sandbox DB - the live service already
has its schema.)

Total served image bytes added: one 64x64 belt texture tile, cropped and
downscaled from the staged `static/factorio/belt/transport-belt.png` sheet,
3.8KB (`static/factorio/belt/belt-tile.png`). The order-10 item icons (ore,
plate, gear, circuit, advanced circuit, rocket part, satellite) are original
inline SVGs, not image assets - the `static/factorio/items/` directory the
lore names as "staged" does not exist in this repo (checked); these are new
art in the same spirit as the existing yard-robot SVGs, not crops of Wube's
item sprites. Zero bytes added there. The full `transport-belt.png` sheet
(1.4MB) stays committed for provenance alongside its crop, matching every
other sprite in `static/factorio/` (raw sheet + cropped subfolder asset), but
is never referenced by the served page.

---

## Order 14 - the inserters must swing

The old `inserter-reach` animation was a fixed 30-degree, 1.6s-lockstep
wiggle - not tied to any data, and every inserter on the strip swung in
perfect unison. Replaced with `inserter-swing`: a ~150-degree arc (`rotate
15deg` to `rotate 165deg`, mirrored for the row-2/left direction), with:

- **Speed bound to the measured `rate_per_hour`** - the exact same
  `--belt-duration` value the belt scroll already uses (order 9's 0.2x-3x
  clamp), set as `--swing-duration` on the same element. An arrow with no
  formal rate instrument never gets a swing at all (`.remnant`, arm frozen) -
  `?`/`n/a`/unmeasured never invents a speed.
- **Staggered phase** - `shipStagePhase(square)` hashes the stage name into a
  stable `[0,1)` value used as a negative `animation-delay` fraction of that
  arrow's own duration, so a row of moving arms is never in lockstep. Hashing
  the name (not threading a numeric index through every call site) satisfies
  the brief's "stable across renders, not random" requirement with less
  invasive plumbing.
- **A backed-up (bottleneck) arrow freezes at the pickup end** - the swing's
  own 0%/100% extreme, `animation:none!important` overriding `.moving` even
  when that same arrow happens to have a positive rate (a slow-but-nonzero
  bottleneck).

Evidence: `swing-frame-1/2/3.png` (three real wall-clock-spaced screenshots -
the CSS animation runs on the browser's own clock, so each is a genuine later
point in the cycle) plus a DOM-level proof in
`tests/dashboard-truth/factorio-belts-evidence.py` that three different
inserters' `getComputedStyle().transform` actually changed across the three
frames, that two inserters with different measured rates are never at the
same rotation at the same frame (staggered), and that `dispatched` (no rate
instrument) never animates while `issues open` (rate 6/h) does.
Mutation-tested per the brief: hardcoding a constant `--swing-duration`/
`--swing-delay` (no rate binding, no stagger) made the "different rates ->
different durations" assertion fail by name
(`AssertionError: ({'duration': '2s', ...}, {'duration': '2s', ...})`), then
restored. Reduced-motion is extended the same way rounds 1-3 already covered
belts: every `.ship-inserter.moving .inserter-arm` reports
`animationName: none` under `prefers-reduced-motion: reduce`.

## Order 15 - BUGS FOUND is a biter

`BUGS FOUND`'s sprite is now chosen by count, from the three staged biter
PNGs, using the **exact same thresholds `bugsCls` already uses** for the
number's own colour (0 neutral / 1-4 warn / >=5 hot) - not an independently
picked number:

- count `0` -> `small-biter-corpse.png` (calm, healthy, nothing attacking)
- `1-4` -> `small-biter.png`
- `>=5` -> `medium-biter.png`
- unknown (no measured count) -> `small-biter.png`, dimmed via the plain
  `.dim` fallback (no remnant art was staged for any biter sprite, so this
  follows the same precedent as every other sprite with no dedicated
  remnant crop, e.g. `inserter.png`) - **never** the corpse asset, per the
  brief's "no bugs and nobody knows must never look alike".

`biter-corpse.png` / `biter-medium.png` / `biter-unknown.png` show the three
states side by side (main `strip-1440-full.png` and the live armbrain shot
carry the "small" state at count 3 and count 25 respectively - the live
shot's medium biter at 25 bugs is visibly angrier/redder than the small one).
Mutation-tested: making the unknown case fall back to
`small-biter-corpse.png` (the same asset as a measured zero) made the
`'corpse' not in unknown_url` assertion fail by name, then restored.

## Order 16 - make it look like the game

Three of the seven differences, in priority order, per the brief ("1, 2 and
3 carry almost all of the effect... do not spread thin across all seven"):

1. **Textured ground.** `static/factorio/ground/ground-tile.png` - a 128x128
   tile cropped from the staged `sand-1.png` sheet (NOT committed, per the
   brief - 8.5MB is too heavy for a background tile, a deliberate departure
   from the belt precedent), grayscaled to keep only the grain/variation,
   then re-tinted into a dark warm brown-black so it reads as ground without
   fighting the neon numerals (**"mute the texture, don't shrink the
   numbers"**) - see the legibility check below. Tiled behind the whole
   strip via `.ship-flow-wrap`'s `background-image`.
2. **Shadows.** A shared `--ground-shadow` CSS custom property
   (`drop-shadow(3px 5px 2px rgba(0,0,0,.55))`), appended to every `filter:`
   rule that touches `.ship-sprite` (`filter` doesn't merge across the
   cascade, so the robot-glow/merge-state/remnant/stalled overrides each
   needed the term added, not just the base rule) plus the inserter
   platform. Same offset and angle everywhere, matching the brief's "all at
   the same angle".
3. **Denser, tighter belts.** Belt width 76px -> 96px, the arrow slot's own
   gap/padding cut from 3px/4px to 1px/2px so the belt and inserter read as
   one mechanism rather than two boxes with a seam, and the item-count
   formulas bumped up a tier (flowing 1-4 -> 2-5, backed-up 5 -> 6, known
   1-3 -> 1-4) for a "packed nose to tail" look on the wider belt.

**Disagreeing with the brief on one point:** "belts run edge to edge... the
width of the screen" is not compatible with keeping the boustrophedon's 15
discrete stage squares and turn arrows (which rounds 1-3 and orders 14-15
all explicitly protect) - a literal single continuous belt would mean
merging or removing stages. Shipped the achievable version instead: denser,
tighter, more continuous-*looking* per-arrow belts, not a structural merge.

**Deferred** (order explicitly allows shipping 1-3 and saying what remains):
foundation pads under machines (4), power poles and wire (5), a broader
density/layout rebalance (6), and scattered ground clutter - rocks, grass
tufts (7). The staged assets for these (`concrete-dummy.png`,
`medium-electric-pole.png`, `big-rock-02/07.png`, `garballo-00/02.png`,
`brown-hairy-grass-00.png`) were inspected but not cropped or committed -
nothing was added to `SOURCE.md` for files this PR doesn't actually ship.

**Legibility check** (the brief's explicit ask, done before calling this
finished): the `.ship-num` overlay plate already sits on its own dark
background per order 11, so the ground texture never touches the digits
directly; `tests/dashboard-truth/factorio-belts-evidence.py` asserts
`BUGS FOUND`/`ISSUES OPEN`/`PRS OPEN`/`CI Q/RUN`'s numerals are non-empty on
the new ground, and every existing screenshot (including the live armbrain
shot) was eyeballed - the cyan/green/yellow/red glow on the Orbitron digits
reads at least as clearly against the new dark textured ground as it did
against flat navy, since the texture's luminance range was deliberately kept
narrow and dark.

Side-by-side comparison with Ben's reference frames, and which differences
remain: see the PR body (image comparison lives there, not in this repo).

## Round 2 review (PR #36) - two blockers, both fixed

1. **`BUGS FOUND` clipped to `UGS FOUND` again** - a regression from order
   16's belt widening (76px -> 96px, arrow slot 132px -> 146px) squeezing row
   1's six stage boxes to 95px, narrower than the caption itself (101px).
   Fixed at the container, not the stages: `.ship-flow-wrap` gained real
   horizontal padding (40px, tuned up from 18px after measuring every one of
   the 15 captions' own margins against `#ship-flow`'s bounding box - 18px
   left "review routed" only 3.5px of margin). Belt/stage widths were left
   untouched this time, since the review didn't flag them.
   `tests/dashboard-truth/factorio-belts-evidence.py` now asserts every
   stage's caption box stays fully inside `#ship-flow`'s own bounds on both
   edges - mutation-tested by zeroing the padding
   (`AssertionError: ('bugs found', 'clipped at left edge', ...)`), restored.
2. **Ground came out dark chocolate brown; Ben's reference is warm light tan
   sand** - his own brief's "legibility beats fidelity" line over-applied:
   round 1 had grayscaled and dark-retinted `ground-tile.png` to protect the
   numerals, solving legibility at the terrain instead of the text. Reverted
   to a completely untouched crop of `sand-1.png` at its own real tone.
   Legibility moved to the text: a new `--text-outline` custom property
   (four-directional dark `text-shadow` plus a soft glow) applied to every
   stage caption, age line, sub-line, legend, and rate label, and
   `.ship-num`'s dark plate opacity bumped `.72` -> `.85`. Re-checked
   afterward: the drop-shadow on every sprite still reads clearly against the
   lighter ground (same offset/angle, just more contrast against sand than it
   had against the old dark retint).

## Order 17 - the real item chain, vertical belts, two inserters

**The item chain.** `SHIP_ARROW_ITEM` replaces round 1's inline-SVG
`SHIP_ARROW_ITEMS` placeholder map with Ben's actual 14-item Factorio chain,
now that `static/factorio/items/` is staged: bug (reusing the order-15 biter
art) -> lab -> copper ore -> copper plate -> copper wire -> green circuit ->
red/advanced circuit -> speed module 1/2/3 -> processing unit -> car -> tank
-> rocket. Ben's own list said "Red Circuit" and "advanced circuit" -
deduped to one item (they're the same item in Factorio), which is exactly
what lands the count on 14 for 14 arrows. Verified against the live
`SHIP_ARROW_ITEM` object via `page.evaluate`, not just read from source, so
the test is an independent check of the mapping, not a copy of it that could
silently drift. Because `gate_verdicts`/`resolved` are permanently-null
fields (Round 3's `n/a` proof), their belts correctly render zero item slots
in the main fixture (never inventing a count) - a separate
`pipeline_fixture_full_chain` (gate_verdicts=3, resolved=2) proves every one
of the 14 belts renders its item when the data exists, and is what
`strip-1440-item-chain.png` was captured against.

**No boxes around belts.** `.ship-belt`'s `outline` rule removed entirely;
the backed-up (jam) state still reads red via glow only
(`box-shadow`), never a border. Asserted for all 14 belts
(`border-style`/`outline-style` both `none`), mutation-tested by re-adding
`border:1px solid #3a4560` - failed by name
(`AssertionError: ('belt has a border', 0, 'solid')`), restored.

**Vertical belts - the honest compromise.** Ben's own brief contained a
self-acknowledged contradiction: "make the belts run up to down" and "belts
... moving right to left but should move left to right" pull in different
directions, and a literal "all belts vertical" would mean abandoning the
boustrophedon's 3-row layout - protected across every review round since
order 11, including this same order 17 document. What's shipped: the two
transitions that are **already geometrically vertical** in the layout
(`review routed` at the end of row 1 dropping into `in review` at the start
of row 2; `resolved` at the end of row 2 dropping into `approved` at the
start of row 3) now render as real vertical belts (`ship-arrow-vertical`,
`ship-belt-vertical`), flowing top to bottom
(`@keyframes ship-belt-flow-vertical`), with the item's own downward motion
matching the visual flow direction. The other 12 within-row handoffs stay
horizontal. This also fixed the belt-direction bug the brief flagged
separately (chevrons scrolling backward against their own arrow) - the
horizontal keyframes' sign was inverted so flow now visibly matches
direction.
Fixing this also surfaced a real bug: `positionShipElbows()` located the two
row-transition anchors via `.ship-stage:last-child`, which broke silently
once the new vertical-arrow element became the actual last child in each
row's HTML (the turn belt was landing wherever the browser's default static
position fell, not tucked against its stage). Fixed by querying
`[data-square="review routed"]` / `[data-square="resolved"]` directly.
Asserted: exactly 2 `.ship-arrow-vertical` elements, both taller than wide;
all other 12 belts wider than tall.

**Two inserters per belt.** Every arrow now renders a loading inserter
(upstream end) and an unloading inserter (downstream end), both bound to the
same measured rate/backed-up/unknown state as order 14 established, each
with its own phase seed (`square + '-load'` / `square + '-unload'`) so they
never move in lockstep. `.inserter-arm`'s base rotation and the
`inserter-swing` keyframes were generalized to a single `--arm-rest` custom
property (0/±90/180deg) so one rule set covers horizontal-right,
horizontal-left, and vertical orientations instead of three separate
keyframe/override pairs. Asserted: exactly 2 `.ship-inserter` children per
arrow (mutation-tested by dropping the unloading inserter - failed by name,
`AssertionError: ('bugs found', 'expected 2 inserters, found', 1)`); both
freeze at the pickup end on the bottleneck arrow; and the loading/unloading
inserters on the same belt carry different `animation-delay` values -
proving they swing on different phases, not as a mirror-synced pair
(mutation-tested by giving both the same phase seed - failed by name,
delays identical at `-2.471s`).

`swing-frame-1/2/3.png` now capture both inserters per tracked arrow;
`strip-1440-item-chain.png` is the "readable bug to rocket" evidence frame.
`static/factorio/SOURCE.md` documents the new `items/` directory.

**Not done / disagreements, stated rather than shipped quietly:** a single
continuous vertical belt spanning all 14 arrows was not attempted beyond the
two transitions above - it would require restructuring the row layout that
every prior round explicitly protected, which this order also explicitly
protects in the same breath it asks for vertical belts. The reference-frame
side-by-side this order also asks for could not be produced - this session
has no access to Ben's actual reference frame images, only his written
description of them (same gap noted in round 2 and order 16's evidence).
