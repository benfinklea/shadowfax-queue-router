Agent badge proof from the scratch Flask application on gandalf, using Ben's
live tmux socket and read-only GitHub calls. The production checkout and daemon
were not edited or restarted.

- `agents.json`: actual `GET /api/agents?fresh=1` response. Rows include idle
  eligible windows (`live: false`); only `live: true` contributes to counts.
- `tmux-windows.txt`: a separate fresh list-windows read. The truth script uses
  `ps` and executable basenames, walking worker ancestry through the pane root,
  to independently establish the live window set.
- `truth.json`: exact census comparison, freshly re-read GitHub target states,
  per-square counts, and headless DOM assertions.
- `pipeline.json`: live pipeline data collected by the scratch API.
- `shipping.png` and `agents-dropdown.png`: headless captures using the above
  real API responses. Synthetic six-square cases run afterward and are not
  represented as live screenshots.

Run with an environment containing the repository requirements and Playwright:

```sh
python tests/dashboard-truth/agents-badge.py
python -m unittest discover -s tests -p test_agents.py
```

Implementation follows the existing strip's armbrain scope and readiness
predicate: fleet-planning lanes remain in the fleet total; green waiting requires
approved, mergeable, CLEAN, non-draft, unheld work. Queue membership takes
precedence over running CI. This is stricter than CLEAN alone in the brief and
agrees with the existing square. Unmatched coordinators are shown as additional
fleet workers so the population is fully accounted for. Failed GitHub mappings
remain live with an unknown square; failed tmux reads return HTTP 503.

Validation: agent, green-waiting and merged-today unit tests passed (14 tests),
as did the existing shipping dropdown/threshold headless regression. The full
unit suite passed 46/47; `test_home_contains_cascade_waterfall` also fails on
base commit 7060bce because the cascade markup it expects is absent there.
