# Review-gate exclusion verification

Captured from the scratch worktree using Flask's in-process test client:
`queue_router.app.test_client().get('/api/pipeline?fresh=1')`.
Only the token provider was replaced with the local `gh auth token` credential;
GitHub data collection ran live without mocked responses.

- `pipeline.json`: available fresh response; green_waiting is 0 and #6840 is absent.
- `pr-6840.json`: independent live read confirms OPEN, APPROVED, MERGEABLE, CLEAN, and both gate-review and galadriel-review labels.
- `live-truth.json`: all five shipping truth checks pass.
- `reinserted-gate-truth.json`: deliberate insertion of #6840 is correctly rejected by the truth check after re-reading GitHub labels. This FAIL is the expected negative-control result.
- DOM evidence: shipping order, tooltip including "no open review gate", thresholds, queue states, and dropdown assertions pass.

Validation commands (using a scratch virtualenv with requirements and Playwright):

```sh
python -m unittest discover -s tests -p test_green_waiting.py
python tests/dashboard-truth/green-waiting-dom.py evidence/strip-gate-labels/pipeline.json evidence/strip-gate-labels
```

All five readiness unit tests pass, including separate exclusions for each review-gate label.
