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

## Real scale (2026-09-12, Ben: "use the real inserter rotation speed and positions from the lua. for the rest of the sprites, too")

One Factorio tile = 24 CSS px on the strip. Every `*-24.png` / `*-sheet.png` below is the game's own hi-res sheet resampled at 0.375 (its Lua `scale = 0.5` x 24/32), so entity footprints are exact: assembling machine 3x3, steel chest 1x1, fast inserter 1x1, belt one tile wide, rocket silo 9x9. Numbers come straight from `base/prototypes/entity/entities.lua` and `transport-belts.lua`: fast-inserter `rotation_speed = 0.04`, `pickup_position = {0,-1}`, `insert_position = {0,1.2}`, platform 105x79 @0.5, hands 72x164 @0.25; assembling-machine-3 32 frames 214x237 @0.5 (8 per line, `animation_speed 0.5`); steel-chest 64x80 @0.5; transport-belt `speed = 0.03125`, frames 128x128 @0.5; rocket-silo layers (shadow, hole, door back/front, base, front) at their `util.by_pixel` shifts.

- `assembler/assembling-machine-3-sheet.png` (640x356, 32 frames of 80x89), `chest/steel-chest-24.png`, `inserter/fast-inserter-platform-24.png` (4 frames of 39x30), `belt/belt-tile-south-24.png` / `belt-tile-north-24.png` (the frame's own 64x64 tile centre), `silo/rocket-silo-24.png` (261x261 composite of the six static layers over the 216px footprint), `ground/ground-grass.jpg` (see below). Source sheets were staged in the session scratchpad only, never committed.

## Directories (keep this list matching what's actually on disk)

- `assembler/`, `belt/`, `chest/`, `inserter/`, `lab/`, `radar/`, `signal/`,
  `silo/` — cropped frames/tiles used by the shipping strip's sprites.
  `inserter/` holds the FAST inserter sheets (platform, hand-base, hand-open,
  hand-closed - 2026-09-12, Ben: "swap out all the long handled inserters
  with fast inserters"); the long-handed sheets they replaced were deleted,
  not kept alongside. `belt/` carries `belt-tile-south-24.png` /
  `belt-tile-north-24.png` (see Real scale above; the earlier 28px
  `belt-tile-vertical*.png` pair was deleted when the strip went to the
  24px tile).
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
  the small derived tile is. `ground-grass.jpg` (Ben, 10:50 AM 2026-09-12:
  "more of a grassland backdrop", then "make the grass better - looks like
  you just used a few instead of rendering a nice background") — a rendered
  4096x2048 field laid the way the game's autoplace does: `grass-1.png`'s
  sixteen 256px variants at random, over-stamped with its 128px (18%) and
  64px (10%) variants, then resampled to 1536x768 at the strip's 0.375
  scale. JPEG (opaque texture).
  The 3.5MB source sheet was staged in the session scratchpad only, never
  committed. Same licensing boundary — internal LAN dashboard only.
- `items/` (order 17, 2026-09-11) — the real Factorio item chain carried
  across the 14 arrows between the 15 stages: `lab.png`, `copper-ore.png`,
  `copper-plate.png`, `copper-cable.png`, `electronic-circuit.png`,
  `advanced-circuit.png` (also stands in for "red circuit" - same item in
  Factorio, deduped rather than drawn twice), `speed-module.png`,
  `speed-module-2.png`, `speed-module-3.png`, `processing-unit.png`,
  `car.png`, `tank.png`, `rocket.png`. The 14th item (the bug at the first
  handoff) reuses `biter/` above rather than a new asset. Same licensing
  boundary as everything else here — internal LAN dashboard only.
