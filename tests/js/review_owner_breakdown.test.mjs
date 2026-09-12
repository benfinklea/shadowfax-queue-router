// Isolated unit test for the client-side per-seat breakdown rendering
// (shipSetBreakdowns / shipBreakdownText / shipSeatRowGlyph / shipOldestText).
// Renders the REAL queue_router.py index() route (via the repo's own venv,
// so Python's string-escape handling is the real one, not approximated by
// a regex over the raw source) and extracts the functions from that actual
// output - these functions have no DOM dependency, so this is a faithful
// test of production code, not a hand-copied mock.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const venvPython = path.join(repoRoot, 'venv', 'bin', 'python');
const python = fs.existsSync(venvPython) ? venvPython : 'python3';

const renderScript = `
import sys
sys.path.insert(0, ${JSON.stringify(repoRoot)})
import queue_router as qr
with qr.app.test_request_context('/'):
    html = qr.index()
print(html if isinstance(html, str) else html.get_data(as_text=True))
`;
const html = execFileSync(python, ['-c', renderScript], { cwd: repoRoot, encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 });
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) throw new Error('could not find <script> block in rendered index() output');
const src = scriptMatch[1];
function extract(name) {
    const start = src.indexOf('function ' + name + '(');
    if (start === -1) throw new Error('not found: ' + name);
    let depth = 0, i = src.indexOf('{', start), end = -1;
    for (; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
    }
    return src.slice(start, end);
}
function extractConst(name) {
    const start = src.indexOf('const ' + name);
    const end = src.indexOf(';', start) + 1;
    return src.slice(start, end);
}

const code = [
    'let shipBreakdowns = {};',
    extract('shipSetBreakdowns'),
    extractConst('SHIP_SEAT_ROW_BUDGET_H'),
    extract('shipSeatRowGlyph'),
    extract('shipOldestText'),
    extract('shipBreakdownText'),
    'globalThis.__api = { shipSetBreakdowns, shipBreakdownText, shipSeatRowGlyph, shipOldestText };',
].join('\n');
(0, eval)(code);
const { shipSetBreakdowns, shipBreakdownText, shipSeatRowGlyph, shipOldestText } = globalThis.__api;

let passed = 0;
function check(name, fn) { fn(); passed++; console.log('PASS', name); }

check('renders per-seat review-routed rows with oldest + fresh glyph', () => {
    shipSetBreakdowns({
        review_routed_breakdown: {
            noun: 'PRs routed', total: null,
            buckets: [
                { label: 'gate', count: 11, oldest_h: 11.8 },
                { label: 'gimli', count: 10, oldest_h: 52.4 },
                { label: 'galadriel', count: 10, oldest_h: 3.1 },
                { label: 'cirdan', count: 1, oldest_h: 131.2 },
            ],
        },
    });
    const text = shipBreakdownText('review routed', 32);
    assert.match(text, /^32 PRs routed/);
    assert.match(text, /gimli · oldest ⛔ 2d 4h/);     // 52.4h > 2x budget(2h) -> hot glyph
    assert.match(text, /galadriel · oldest ⚠ 3h 6m/); // 3.1h: over budget, under 2x -> amber glyph
    assert.match(text, /gate · oldest ⛔ 11h 48m/);    // 11.8h > 2x budget(2h) -> hot glyph
});

check('stale-verdict row always renders the hot glyph regardless of age', () => {
    shipSetBreakdowns({
        stale_verdicts_breakdown: {
            noun: 'PRs', total: null,
            buckets: [{ label: 'gimli', count: 1, oldest_h: 45.7 }],
        },
    });
    // Production always calls shipStage(gvNa ? 'n/a' : d.gate_verdicts, ...) -
    // gate_verdicts is permanently null/n-a on this repo (statusCheckRollup
    // dropped), so 'n/a' is the real num shipBreakdownText sees here, not null.
    const text = shipBreakdownText('gate verdicts', 'n/a');
    assert.match(text, /^n\/a PRs/);
    assert.match(text, /1 gimli · oldest ⛔ 45h 42m/);
});

check('a bucket with count 0 is never rendered (never fabricate a waiting seat)', () => {
    shipSetBreakdowns({
        in_review_breakdown: {
            noun: 'PRs', total: null,
            buckets: [{ label: 'author', count: 0, oldest_h: 5 }, { label: 'gimli', count: 2, oldest_h: 9 }],
        },
    });
    const text = shipBreakdownText('in review', 2);
    assert.ok(!text.includes('author'), 'zero-count row must not appear');
    assert.match(text, /gimli/);
});

check('a null oldest_h on a real row renders SKIP, never 0h', () => {
    shipSetBreakdowns({
        review_routed_breakdown: { noun: 'PRs routed', total: null, buckets: [{ label: 'cirdan', count: 1, oldest_h: null }] },
    });
    const text = shipBreakdownText('review routed', 1);
    assert.match(text, /oldest SKIP/);
    assert.ok(!text.includes('oldest 0'));
});

check('no breakdown payload renders empty string (existing squares unaffected)', () => {
    shipSetBreakdowns({});
    assert.equal(shipBreakdownText('review routed', 5), '');
});

console.log(`\n${passed} passed`);
