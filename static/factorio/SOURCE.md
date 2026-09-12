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
- `remnants/` — wreckage art shown for a stage/arrow with no measured data.
- `biter/` (order 15, 2026-09-11) — `small-biter.png`, `medium-biter.png`,
  `small-biter-corpse.png`. Used for the `BUGS FOUND` stage: corpse at a
  measured zero, small/medium biter above that by count. Same licensing
  boundary as everything else here — internal LAN dashboard only.
- `ground/` (order 16, 2026-09-11) — `ground-tile.png`: a 128x128 tile
  cropped from `sand-1.png` (a terrain sheet staged at `/tmp/factorio-raw/`
  on frodo, NOT committed here per order 16 - 8.5MB is too heavy for a
  repeating background tile), grayscaled and re-tinted into the dashboard's
  own dark palette so it reads as textured ground without fighting the
  Orbitron numerals. The source sheet itself was never committed or copied
  into this repo - only the small derived tile is.
