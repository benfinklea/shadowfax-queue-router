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
