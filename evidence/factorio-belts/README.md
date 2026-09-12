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
protects in the same breath it asks for vertical belts.

## Reference-frame comparison (Ben supplied three screenshots after this
## session first shipped, correcting the earlier "no access" note)

`reference-comparison.png` stacks one of Ben's real-gameplay reference frames
over `strip-1440-item-chain.png`. Pulled from `Bens-Mac.local` over SSH (the
paths he pasted are local to his Mac, unreachable from this worktree's own
box - `rsync`/`scp` both choke on the filename's spaces even quoted, so the
working path was `ssh ... 'for f in *pattern*; do base64 -i "$f"; done'` on
the remote side, piped back and decoded locally).

What the comparison actually found, most significant first:

1. **Belts weren't reading as belts at all - fixed.** `static/factorio/belt/
   belt-tile.png` was a 64x64 tile whose real belt-tread art only filled a
   40x40 box in the middle (`Image.getbbox()` confirmed the transparent
   margin) - tiled at 18px, most of what rendered was transparent, so items
   floated on bare sand between faint arrow specks instead of a visible
   moving surface. Re-cropped from the raw `transport-belt.png` sheet
   (already staged, never referenced by the previous crop) to a tight 68x68
   tile with almost no dead margin. Same fix also exposed and corrected a
   real bug on the two vertical belts: `.ship-belt-vertical .ship-belt-track`
   inherited `background-repeat:repeat-x` from the base rule, so a vertical
   belt only ever painted ONE 18px band of texture and left the rest of its
   72px height as bare sand - added `background-repeat:repeat` so the tread
   now fills the whole vertical run.
2. **Ground tone** - close now (both a warm light tan), though the reference
   is slightly more desaturated/grey with visible tire-track wear; not
   changed further, since chasing an exact match risks re-fighting the
   legibility problem round 2 already solved at the text.
3. **Inserter color** - the reference's basic inserters are yellow; this
   dashboard uses the long-handed-inserter sprite (authentically
   orange/copper in Factorio, just a different tier), established in order
   14 and left as-is since neither order asked for a tier change.
4. **Density/scale** - the reference is one tightly-packed factory block; this
   strip is 15 discrete, widely-spaced stages by design (order 16 already
   disagreed with "edge to edge" density for the same structural reason as
   the vertical-belt compromise above).
5. **Not shipped, unchanged from order 16's deferred list**: power poles and
   wire, foundation pads, ground clutter (grass tufts, loose rock) - the
   reference frames confirm all three are visible in real play and still
   absent here.

Chevron direction is one known remaining nit from the belt-tile fix: the
tile's arrows point sideways (rightward) even on the two vertical belts,
since rotating just the background pattern (not the whole element) isn't a
plain CSS operation - flagged rather than left silent, not blocking.

## Ben's live session, 2026-09-11 evening -> 2026-09-12 (after order 17)

Ben drove this round directly from his own Factorio builds (screenshots pulled from `Bens-Mac.local` over SSH each time). Every item below is a change he asked for by looking at a render, in order:

- **All 14 belts vertical.** His custom rig showed a vertical belt bridging two side-by-side buildings, so the boustrophedon never had to change - only the belt inside each arrow slot did (`.ship-arrow-vbelt`, in-row, normal flow; `.ship-arrow-vertical` stays the row-end column).
- **Inserters flank the belt left/right**, reaching both the stage before and the stage after; each sits at the belt's own beginning or end (top/bottom by flow direction), never centered beside it. Row 2 (row-reversed) mirrors so the loading inserter is on the upstream (right) side.
- **Belts alternate direction** along a row (chevrons and item motion): row 1 starts down; row 2 starts UP because the row-end belt feeding it comes down. Row-end belts always flow down.
- **Belts are queues.** A belt shows one item per job waiting to enter the next stage (`arrow.backlog`, cap 7), piled from the exit end. The stage number is jobs being worked; rate only sets scroll speed. `CI Q/RUN` became `CI RUN` with its queue on the belt to its left (server: prs-ci backlog = `ci_queued`).
- **7 / 7 / 1 rows**, row-end belts run beside the last stage all the way down to beside the next row's first stage (JS-sized sprite-top to sprite-bottom, re-run after fonts load), rows 56px apart, DEPLOYED lined up under FOLDED.
- **Real belt art.** The first vertical tile was the horizontal frame rotated (one rail, seams). Now the sheet's own north/south frames, one 64px tread period, pre-scaled to the 28px belt width, `repeat-y`.
- **Captions and time stamps on a dark plate**, white text (Ben could not read the grey-on-sand).
- **Assemblers where work happens, chests where it waits**; biter and silo unchanged.
- **Fast inserters** (blue) replace the long-handed ones - same sheet layout, art swapped, long-handed files deleted. The arm pivots on the tripod's bearing (measured in the platform frame), is ~1.5x the tripod's width like his close-up, swings the **full 180** (pickup one side, drop the other, over the top), and switches hand sprite: closed while carrying to the drop, open on the way back.
- **Remnant** now means only "the count is n/a" - it was wrecking every un-instrumented handoff even with a known count. Not time-based.
- **Closed-issues list** on row 3: the 10 most recently closed issues with hh:mm bug found -> dispatched and dispatched -> deployed. Dispatched = lane launch time when recorded, else the first commit on the fixing PR (no per-issue dispatch ledger exists on disk); deployed = first green production deploy run started after that PR merged. n/a for any unmeasured leg. Verified against live GitHub (e.g. #6976: 186:19 / 05:51).

Every change above has a named assertion in `tests/dashboard-truth/factorio-belts-evidence.py` (row counts, row-end column geometry and in-frame, chevron sequence per row, loading-inserter side per row, queue items, closed-issues panel, 180-degree keyframes, fast-inserter platforms, remnant-only-when-n/a). Suite green on every push; screenshots regenerated each run and synced to Ben's Mac.

## Ben's 10:08 AM rig, 2026-09-12 - the conflict loop and DEPLOYED on row 2

Ben: "From gate verdicts, I'd like to split conflicted to go straight down, and then have resolved come in from the left. When things are resolved, they get passed back up to Approved. That means we have extra room on row 2 so we can fit deployed on row 2." His screenshot (`Screenshot 2026-09-12 at 10.08.39 AM.png`, read belt by belt at native resolution for chevron direction) is the layout; the words are the semantics.

- **Rows are 7 / 6 / 2.** Row 2 (right to left under IN REVIEW): gate verdicts, approved, in queue, merged, folded, DEPLOYED. Row 3 is the conflict loop hanging under the right end of row 2: CONFLICTED directly under gate verdicts, RESOLVED directly under approved (row 3 is row-reversed too; each row-3 box is width-matched to the row-2 box above it so the machines line up column for column). The closed-issues list moved to row 3's open space, now on the left.
- **Gate verdicts splits.** The right-hand column is one continuous belt from IN REVIEW down past GATE VERDICTS to CONFLICTED - built as `#ship-elbow-1` (ends at gate verdicts' middle) butted onto `#ship-elbow-3` (starts there) at the same x, with the unloader into the gate stacked over the loader out of it, exactly as in the rig. Both belts leaving the gate carry its item (speed module); the down belt shows the conflicted count, the left belt the gate's own.
- **Resolved rides approved's belt back up.** `#ship-elbow-4` is one tall belt from beside RESOLVED (row 3) up past APPROVED to IN QUEUE, flowing UP (row 2's chevrons now read down, up, down, up, down off his rig). Three inserters: approved loads it level with its own machine, resolved's feeder loads it at the very bottom (the belt's start), and the unloader at the top-left is level with IN QUEUE. Approved's queue (green-inline backlog, processing units) piles from the top; resolved's items (speed module 3) queue behind. Capacity 14 on this double-height belt. Both rows reserve the column with a `.ship-elbow-slot` so nothing paints under it.
- **Chevrons:** every row starts down now (`SHIP_VBELT_ROW_ORDER` lists 'approved' as row 2's "up"); the row-end column belts flow down, the shared belt is the one that flows up.

Mutation pass on the new assertions (server restarted per mutant; the first run with a stale server was a false green and is why the restart is scripted now): shared belt forced down - killed (chevron sequence); elbow-3 moved 12px/8px off elbow-1 - killed ("top misplaced"); resolved items dropped from the shared belt - killed (item chain). Two equivalent survivors, documented: removing the approved/resolved width match (both boxes already sit at the same min-width in the fixture) and pinning the unloader to the belt top (IN QUEUE's machine top already IS the belt top in the fixture geometry) - both guards exist for live payloads where a sub-line changes a box's size.

## Ben, 10:45-10:50 AM CDT 2026-09-12 - items in the hand, grassland

- **The work product rides in the hand.** Ben: "when the inserters pass on a work product, have it move with the inserter's grasping arm and drop onto the start of the belt ... the work object should move with the grasping hand into the next stage." Each moving inserter's arm now carries the arrow's own item at the hand tip (`.inserter-item`, a child of the arm so it rotates with it) for the carrying half of the swing - pickup side to drop side, hand closed - and it vanishes at the drop, where the hand opens for the empty return. Loader and unloader both, and resolved's feeder. Same duration and delay as the arm, so it is the arm's own swing. Idle inserters hold nothing. Reduced motion: item shown still, no animation. Mutants: item removed - killed ("moving inserter carries no item"); item put on its own clock (delay forced to 0) - killed ("item not on the arm's own swing").
- **Grassland.** Ben: "give it more of a grassland backdrop - look in the art folder." `static/factorio/ground/ground-grass.jpg` is two 1024x256 runs of the game's own `grass-1.png` (the big blended variants with dirt patches and tufts his rig sits on), stacked, at half scale so a patch is about a machine and a half wide. Captions and numbers still sit on their dark plates, so legibility is unchanged.
