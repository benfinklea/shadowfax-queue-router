# Sprite source

These PNGs are cropped from Ben's own local Factorio install:
`/Applications/factorio.app/Contents/data/base/graphics/icons/` and
`/Applications/factorio.app/Contents/data/base/graphics/entity/...`.

They are copyright Wube Software. Ben owns a licensed copy of the game;
this dashboard is internal, runs on his LAN, and sits behind no public
URL, so this is ordinary personal use of assets he already paid for.

**Internal-only, permanently:**
- Never publish these on armbrain.io, the HQ portal, marketing, or any
  customer-facing surface.
- Never commit them to a public repo. This repo (`shadowfax-queue-router`)
  is private — keep it that way.
- Do not copy these files into any other project without re-checking this
  note applies there too.

## Directories (keep this list matching what's actually on disk)

- `assembler/`, `belt/`, `chest/`, `inserter/`, `lab/`, `radar/`, `signal/`,
  `silo/` — cropped frames/tiles used by the shipping strip's sprites.
  `inserter/` holds the FAST inserter sheets (platform, hand-base, hand-open,
  hand-closed - 2026-09-12, Ben: "swap out all the long handled inserters
  with fast inserters"); the long-handed sheets they replaced were deleted,
  not kept alongside. `belt/` also carries `belt-tile-vertical.png` /
  `belt-tile-vertical-up.png`: the sheet's own south/north straight-belt
  frames cropped to one 64px tread period and pre-scaled to the strip's
  28px belt width.
- `remnants/` — wreckage art shown for a stage/arrow with no measured data.
- `biter/` (order 15, 2026-09-11) — `small-biter.png`, `medium-biter.png`,
  `small-biter-corpse.png`. Used for the `BUGS FOUND` stage: corpse at a
  measured zero, small/medium biter above that by count. Same licensing
  boundary as everything else here — internal LAN dashboard only.
- `ground/` (order 16, 2026-09-11) — `ground-tile.png`: a 128x128 tile
  cropped from `sand-1.png` (a terrain sheet staged at `/tmp/factorio-raw/`
  on frodo, NOT committed here per order 16 - 8.5MB is too heavy for a
  repeating background tile), at its own real colour - no grayscale, no
  re-tint (round 2 on PR #36: a first pass darkened it to protect the
  numerals, which stopped it looking like Factorio's own sand; legibility
  is solved at the text now - see `--text-outline` and `.ship-num` in
  `queue_router.py` - so the ground stays close to its real tone). The
  source sheet itself was never committed or copied into this repo - only
  the small derived tile is.
- `items/` (order 17, 2026-09-11) — the real Factorio item chain carried
  across the 14 arrows between the 15 stages: `lab.png`, `copper-ore.png`,
  `copper-plate.png`, `copper-cable.png`, `electronic-circuit.png`,
  `advanced-circuit.png` (also stands in for "red circuit" - same item in
  Factorio, deduped rather than drawn twice), `speed-module.png`,
  `speed-module-2.png`, `speed-module-3.png`, `processing-unit.png`,
  `car.png`, `tank.png`, `rocket.png`. The 14th item (the bug at the first
  handoff) reuses `biter/` above rather than a new asset. Same licensing
  boundary as everything else here — internal LAN dashboard only.
