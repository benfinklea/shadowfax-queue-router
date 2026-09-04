# Shipping strip evidence

Captured 2026-09-04 from the scratch worktree. The production checkout and service were not changed or restarted.

`pipeline.json` is the HTTP 200 response from the scratch Flask app's `/api/pipeline?fresh=1` route through `app.test_client()`, with real upstream GitHub reads and no mocked data. It reports `available: true`, `queue_depth: 0`, and `queue_prs: []`.

`live-truth.json` records an independent live GitHub re-read using the extended `check_green_waiting()` truth check: all four checks passed, including ordered queue entries and count equality. These are point-in-time observations; the normal pipeline cache is 45 seconds.

`shipping.png` and `shipping.html` capture the scratch renderer with that saved API response in headless Chromium. The real CSS, renderer, help text, and logo are used. `dom-checks.json` asserts the literal captions and existing green-waiting thresholds. `queue-checks.json` records synthetic 0/1/2-entry and stuck-entry scenarios, including a stuck second entry; colors, count, subtitle and exact tooltip all passed. Synthetic scenarios are separate from the screenshot's live data.

Reproduce the browser checks:

```sh
python tests/dashboard-truth/green-waiting-dom.py evidence/strip-order/pipeline.json /tmp/strip-order-capture
```

Additional validation: all five `test_green_waiting.py` unit tests passed; Python compilation and `git diff --check` passed. The broader fleet truth suite was not run; only the relevant shipping checks were run.
