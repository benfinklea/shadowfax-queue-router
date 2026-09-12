"""Headless proof that the PRS OPEN hover breakdown is SCREENSHOT-CAPTURABLE.
A native title= tooltip is painted by the OS compositor and is invisible here;
this asserts the bubble is real DOM, then captures it as pixels."""
import sys, json
from playwright.sync_api import sync_playwright

CHROME = "/home/ben/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"
URL = "http://gandalf.local:5000/"
OUT = "/tmp/stage-hover"
fails = []

with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1920, "height": 1200})
    pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
    # Wait for the strip to have a real number in the PRS OPEN tile.
    pg.wait_for_selector('.ship-stage[data-square="prs open"]', timeout=90000)
    pg.wait_for_function(
        """() => { const e=document.querySelector('.ship-stage[data-square="prs open"] .ship-num');
                   return e && /\\d/.test(e.textContent); }""", timeout=90000)

    tile = pg.query_selector('.ship-stage[data-square="prs open"]')
    print("tile number rendered:",
          pg.eval_on_selector('.ship-stage[data-square="prs open"] .ship-num', 'e=>e.textContent.trim()'))

    # The attribute must exist, and the native title must be GONE on this tile.
    attr = tile.get_attribute("data-ship-breakdown")
    title = tile.get_attribute("title")
    print("data-ship-breakdown attr:", json.dumps(attr))
    print("native title on tile     :", json.dumps(title))
    if not attr:
        fails.append("tile has no data-ship-breakdown attribute")
    if title:
        fails.append("tile still carries a native title= (OS tooltip would shadow the DOM one)")

    tile.hover()
    pg.wait_for_selector("#ship-breakdown-tooltip.visible", timeout=15000)
    tip = pg.query_selector("#ship-breakdown-tooltip")
    text = tip.inner_text()
    box = tip.bounding_box()
    print("--- tooltip inner_text ---"); print(text); print("--------------------------")
    print("tooltip box:", box)

    # It must be genuinely painted, not merely present.
    vis = pg.evaluate("""() => { const t=document.getElementById('ship-breakdown-tooltip');
        const s=getComputedStyle(t), r=t.getBoundingClientRect();
        return {display:s.display, opacity:s.opacity, visibility:s.visibility, w:r.width, h:r.height}; }""")
    print("computed:", vis)
    if vis["display"] == "none" or float(vis["opacity"]) < 0.9 or vis["w"] < 40 or vis["h"] < 20:
        fails.append(f"tooltip not actually painted: {vis}")

    lines = [l for l in text.split("\n") if l.strip()]
    if not lines or " PRs" not in lines[0]:
        fails.append(f"header line wrong: {lines[:1]}")
    nums = []
    for l in lines[1:]:
        tok = l.split(" ")[0]
        if tok.isdigit():
            nums.append(int(tok))
    total = int(lines[0].split(" ")[0])
    if sum(nums) != total:
        fails.append(f"tooltip lines sum to {sum(nums)}, not {total}")
    else:
        print(f"SUM IN THE RENDERED IMAGE: {'+'.join(map(str,nums))} = {sum(nums)} == {total}  OK")
    if any(l.startswith("0 ") for l in lines[1:]):
        fails.append("a zero bucket is rendered")

    pg.screenshot(path=f"{OUT}-full.png")
    # Crop generously around the tooltip so the text is legible in the artifact.
    pad = 18
    pg.screenshot(path=f"{OUT}-tooltip.png", clip={
        "x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
        "width": box["width"] + pad * 2, "height": box["height"] + pad * 2})
    # Control shot: mouse away, tooltip must disappear (proves hover drives it).
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(400)
    still = pg.evaluate("""() => document.getElementById('ship-breakdown-tooltip')
                             .classList.contains('visible')""")
    print("tooltip still visible after mouse-away:", still)
    if still:
        fails.append("tooltip did not hide on mouseout")
    pg.screenshot(path=f"{OUT}-nohover.png")
    b.close()

print()
if fails:
    print("FAIL"); [print("  -", f) for f in fails]; sys.exit(1)
print("PASS - breakdown is real DOM, painted, hover-driven, sums correctly, and captured")
