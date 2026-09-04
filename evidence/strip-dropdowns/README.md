# Shipping dropdown evidence

Captured from the scratch PR #17 worktree on 2026-09-04. No live checkout edits or service restarts.

`pipeline.json` is the HTTP 200 JSON response from the scratch Flask test client's `/api/pipeline?fresh=1`, using real upstream reads. Counts: green waiting 1, queue 1, merged today 30. `shipping.png` shows the open merged-today dropdown using that exact response. The list scrolls; all 30 rows exist in `shipping.html`.

`dropdown-checks.json` records headless Chromium assertions for every list: row count equals its square and payload list length; row PR numbers, link text and href agree; links have target=_blank and rel=noopener; titles are at most 15 characters. All lists start closed, only one opens at once, clicking again closes, and open/closed state survives the refresh callback. The script asserts the production polling interval is 60000 ms and invokes its callback directly. Additional fixtures verify word-boundary/hard title cuts, HTML escaping, and testing/stuck queue labels.

The capture loads the real Flask-rendered shipping JavaScript and stylesheet into an isolated headless page. Fetch returns the saved live payload; the real logo is embedded to avoid needing a running server. `dom-checks.json` and `queue-checks.json` retain stage-order, threshold, queue color and subtitle checks. `live-truth.json` contains the five passing independent shipping checks, including a fresh GitHub queue/readiness re-read. These are point-in-time observations.

Reproduce:

```sh
python tests/dashboard-truth/green-waiting-dom.py evidence/strip-dropdowns/pipeline.json /tmp/dropdown-capture
python -m unittest discover -s tests -p 'test_green_waiting.py'
python -m unittest discover -s tests -p 'test_merged_today.py'
```

Nine Python tests passed, including merged search pagination, merge timestamp ordering, empty lists, HTTP errors and incomplete/truncated/duplicate result rejection. Python compilation and git diff --check passed. The broader fleet truth suite was not run.
