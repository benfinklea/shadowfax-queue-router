"""Headless shipping capture: python green-waiting-dom.py PAYLOAD_JSON OUTPUT_DIR."""
import base64
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
import queue_router as qr

payload = json.loads(Path(sys.argv[1]).read_text())
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
page_html = qr.app.test_client().get('/').get_data(as_text=True)
source = (root / 'queue_router.py').read_text()
renderer = page_html[page_html.index('function sparkHtml('):page_html.index('function refreshCiQueue(')]
help_text = source[source.index('const HELP = {'):source.index('\n};', source.index('const HELP = {')) + 3]
styles = '\n'.join(re.findall(r'<style>(.*?)</style>', page_html, re.S))
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1600, 'height': 420})
    page.set_content('<style>' + styles + '</style><div id="ship-flow" class="ship-flow"></div>')
    page.evaluate('(d) => {window.payload=d; window.fetch=async()=>({json:async()=>window.payload});}', payload)
    page.add_script_tag(content=help_text + '\n' + renderer)
    page.evaluate('refreshShipFlow()')
    page.wait_for_selector('.ship-stage')
    stages = page.locator('.ship-cap').all_text_contents()
    assert stages == ['issues open', 'prs open', 'ci q/run', 'green waiting', 'in line', 'merged today', 'last deploy (CT)'], stages
    assert page.locator('.ship-arrow').count() == len(stages) - 1
    assert 'updated' not in page.locator('#ship-flow').inner_text().lower()
    assert page.locator('.ship-updated').count() == 0
    assert 'every required test passing' in page.locator('.ship-stage').filter(has=page.locator('.ship-cap', has_text='green waiting')).get_attribute('title')
    logo = 'data:image/svg+xml;base64,' + base64.b64encode((root / 'armbrain-logo.svg').read_bytes()).decode()
    page.locator('.ship-logo').evaluate('(el, src) => { el.src = src; }', logo)
    page.wait_for_function('document.querySelector(".ship-logo").complete')
    assert page.locator('.ship-toggle').count() == 3
    assert page.locator('.ship-dropdown:visible').count() == 0
    dropdown_checks = []
    for key, field in [('green-waiting', 'green_waiting_prs'), ('in-line', 'queue_prs'), ('merged-today', 'merged_today_prs')]:
        toggle = page.locator('[data-dropdown="' + key + '"]')
        toggle.click()
        panel = page.locator('#ship-list-' + key)
        assert panel.is_visible()
        assert page.locator('.ship-dropdown:visible').count() == 1
        stage = page.locator('.ship-stage').filter(has=toggle)
        rows = panel.locator('.ship-pr')
        assert rows.count() == int(stage.locator('.ship-num').inner_text()) == len(payload[field])
        for row, pr in zip(rows.all(), payload[field]):
            link = row.locator('a')
            assert row.get_attribute('data-pr-number') == str(pr['number'])
            assert link.inner_text() == '#' + str(pr['number'])
            assert link.get_attribute('href') == 'https://github.com/armbrain-io/armbrain/pull/' + str(pr['number'])
            assert link.get_attribute('target') == '_blank'
            assert link.get_attribute('rel') == 'noopener'
            assert len(row.locator('span').first.inner_text()) <= 15
        # Invoke the exact callback scheduled every 60 seconds, without waiting a minute.
        page.evaluate('refreshShipFlow()')
        page.wait_for_function('(key) => document.querySelector("[data-dropdown=" + key + "]").getAttribute("aria-expanded") === "true"', arg=key)
        assert panel.is_visible()
        dropdown_checks.append({'key': key, 'rows': rows.count(), 'links': 'PASS', 'refresh': 'PASS'})
    assert 'setInterval(refreshShipFlow, 60000)' in source
    # Refresh recreates the logo; embed it again for this isolated capture.
    page.locator('.ship-logo').evaluate('(el, src) => { el.src = src; }', logo)
    page.wait_for_function('document.querySelector(".ship-logo").naturalWidth > 0')
    # Capture the open merged-today dropdown with the unmodified live payload.
    out.joinpath('shipping.html').write_text(page.locator('#ship-flow').evaluate('(el)=>el.outerHTML'))
    page.screenshot(path=str(out / 'shipping.png'))
    page.locator('[data-dropdown=merged-today]').click()
    assert page.locator('.ship-dropdown:visible').count() == 0
    page.evaluate('refreshShipFlow()')
    assert page.locator('.ship-dropdown:visible').count() == 0
    assert page.evaluate("[shipShortTitle('short title'), shipShortTitle('one two three four'), shipShortTitle('abcdefghijklmnop'), shipShortTitle('abcdefghijklmno p')]") == ['short title', 'one two three', 'abcdefghijklmno', 'abcdefghijklmno']
    page.evaluate("() => { window.payload = {...window.payload, green_waiting: 1, green_waiting_prs: [{number:123,title:'<img onerror=x>'}]}; refreshShipFlow(); }")
    assert page.locator('#ship-list-green-waiting img').count() == 0
    assert page.locator('#ship-list-green-waiting .ship-pr span').inner_text() == '<img onerror=x>'
    out.joinpath('dropdown-checks.json').write_text(json.dumps(dropdown_checks, indent=2))
    checks = []
    for n, expected in [(0, ''), (1, 'ok'), (2, 'ok'), (3, 'warn'), (5, 'warn'), (6, 'hot'), (9, 'hot')]:
        page.evaluate('(n)=>{window.payload={...window.payload,green_waiting:n,green_waiting_prs:Array.from({length:n},(_,i)=>({number:123+i,title:"fixture"}))};refreshShipFlow();}', n)
        stage = page.locator('.ship-stage').filter(has=page.locator('.ship-cap', has_text='green waiting'))
        cls = stage.locator('.ship-num').get_attribute('class').strip()
        assert cls == ('ship-num ' + expected).strip(), (n, cls)
        assert stage.locator(':scope > .ship-sub').all_text_contents() == (['#123'] if n else [])
        assert stage.locator('.ship-spark').count() == 0
        checks.append({'count': n, 'class': expected or 'neutral'})
    queue_checks = []
    for entries, expected, subtitle in [
        ([], 'hot', []),
        ([{'number': 42, 'state': 'AWAITING_CHECKS'}], 'warn', ['#42']),
        ([{'number': 42, 'state': 'AWAITING_CHECKS'}, {'number': 7, 'state': 'QUEUED'}], 'ok', ['#42']),
        ([{'number': 42, 'state': 'AWAITING_CHECKS'}, {'number': 7, 'state': 'UNMERGEABLE'}], 'hot', ['#7 stuck']),
        ([{'number': 7, 'state': 'UNMERGEABLE'}], 'hot', ['#7 stuck']),
    ]:
        page.evaluate('(entries)=>{window.payload={...window.payload,queue_depth:entries.length,queue_prs:entries};refreshShipFlow();}', entries)
        stage = page.locator('.ship-stage').filter(has=page.locator('.ship-cap', has_text='in line'))
        assert stage.locator('.ship-pr').count() == len(entries)
        assert stage.locator('.ship-pr .ship-sub').all_text_contents() == [('stuck' if pr['state'] == 'UNMERGEABLE' else 'testing') for pr in entries]
        assert stage.locator('.ship-num').get_attribute('class') == 'ship-num ' + expected
        assert stage.locator('.ship-num').inner_text() == str(len(entries))
        assert stage.locator(':scope > .ship-sub').all_text_contents() == subtitle
        assert stage.get_attribute('title') == 'IN LINE - pull requests the merge queue is testing right now. Two is full and good. Zero means nothing is being merged.'
        queue_checks.append({'entries': entries, 'class': expected, 'subtitle': subtitle})
    out.joinpath('queue-checks.json').write_text(json.dumps(queue_checks, indent=2))
    out.joinpath('dom-checks.json').write_text(json.dumps({'stages': stages, 'thresholds': checks}, indent=2))
    print(json.dumps({'stages': stages, 'thresholds': checks}))
    browser.close()
