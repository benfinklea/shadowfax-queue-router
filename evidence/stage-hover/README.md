# STAGE-HOVER - the PRS OPEN hover breakdown

Ben, 2026-09-12: "Hovering a stage on the CI/CD pipeline should show a breakdown of what is inside it."

`stage-hover-tooltip.png` is the crop that matters: the bubble, captured HEADLESSLY by Playwright on frodo. That is the whole point of the feature. The two earlier SHIP-SPARK attempts used a native SVG `<title>`, which the OS compositor paints - real hover shows it, a screenshot never can, and Ben reads this dashboard largely through screenshots an agent captures. This bubble is real DOM, so it lands in the PNG.

`stage-hover-full.png` is the same moment at 1920x1200, showing the bubble in place on the strip.

Captured against the live dashboard at 14:14 UTC 2026-09-12 with 63 open PRs on armbrain-io/armbrain:

```
63 PRs
24 drafts
14 human-gated
1 conflicted
16 unreviewed
7 approved, checks failing
1 ready to merge
```

24+14+1+16+7+1 = 63. The buckets are first-match over an ordered list, so they always sum to the total.

Reproduce:

- `./venv/bin/python tests/dashboard-truth/stage-hover.py` - offline fixture + mutation guard, no network, no browser.
- `python3 tests/dashboard-truth/stage-hover-dom.py` - on frodo; hovers the live tile and asserts the bubble is painted, hover-driven, and sums, then writes the PNGs to /tmp.
