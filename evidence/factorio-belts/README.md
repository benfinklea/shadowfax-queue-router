Evidence for LORE order 11 ("the sprite is the square") - the visual rebuild
of the 15-stage strip: card removal, real belts carrying the order-10 item
chain, and rate-driven animation. Captured against this worktree's own Flask
process (port 5099), not the live `queue-router.service` - that was never
touched or restarted.

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
  wreckage sprite, `?` numeral) - proving the three states stay visually
  distinct, per the lore's explicit "must never look alike" rule.
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
