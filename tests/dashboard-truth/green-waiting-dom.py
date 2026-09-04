"""Headless shipping capture: python green-waiting-dom.py PAYLOAD_JSON OUTPUT_DIR."""
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
renderer = source[source.index('function sparkHtml('):source.index('function refreshCiQueue(')]
help_text = source[source.index('const HELP = {'):source.index('\n};', source.index('const HELP = {')) + 3]
styles = '\n'.join(re.findall(r'<style>(.*?)</style>', page_html, re.S))
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1600, 'height': 300})
    page.set_content('<style>' + styles + '</style><div id="ship-flow" class="ship-flow"></div>')
    page.evaluate('(d) => {window.payload=d; window.fetch=async()=>({json:async()=>window.payload});}', payload)
    page.add_script_tag(content=help_text + '\n' + renderer)
    page.evaluate('refreshShipFlow()')
    page.wait_for_selector('.ship-stage')
    stages = page.locator('.ship-cap').all_text_contents()
    assert 'green waiting' in stages and 'deployed today' not in stages, stages
    assert page.locator('.ship-arrow').count() == len(stages) - 1
    assert 'updated' not in page.locator('#ship-flow').inner_text().lower()
    assert page.locator('.ship-updated').count() == 0
    assert 'every required test passing' in page.locator('.ship-stage').filter(has=page.locator('.ship-cap', has_text='green waiting')).get_attribute('title')
    out.joinpath('shipping.html').write_text(page.locator('#ship-flow').evaluate('(el)=>el.outerHTML'))
    page.locator('#ship-flow').screenshot(path=str(out / 'shipping.png'))
    checks = []
    for n, expected in [(0, ''), (1, 'ok'), (2, 'ok'), (3, 'warn'), (5, 'warn'), (6, 'hot'), (9, 'hot')]:
        page.evaluate('(n)=>{window.payload={...window.payload,green_waiting:n,green_waiting_prs:n?[{number:123,title:"fixture"}]:[]};refreshShipFlow();}', n)
        stage = page.locator('.ship-stage').filter(has=page.locator('.ship-cap', has_text='green waiting'))
        cls = stage.locator('.ship-num').get_attribute('class').strip()
        assert cls == ('ship-num ' + expected).strip(), (n, cls)
        assert stage.locator('.ship-sub').all_text_contents() == (['#123'] if n else [])
        assert stage.locator('.ship-spark').count() == 0
        checks.append({'count': n, 'class': expected or 'neutral'})
    out.joinpath('dom-checks.json').write_text(json.dumps({'stages': stages, 'thresholds': checks}, indent=2))
    print(json.dumps({'stages': stages, 'thresholds': checks}))
    browser.close()
