# HOME LAB source evidence

Captured 2026-09-04 at 22:39 UTC from the scratch worktree.

- `live.json`: real service-token organization inventory (permission now succeeds).
- `job-history.json`: scratch API fallback response; ONLY org HTTP 403 is simulated. All workflow/job data and host names are live.
- `truth.json`: independent fresh `gh api` reads match history busy=31 and inventory online=76. Recent runs of every status are inspected because queued/rerun workflows may expose in-progress jobs.
- `job-history.png`, `live.png`: browser rendering of these payloads; no fixture runner data. Browser checks validate source labels, exact caption, unknown values, hosts and counts.

The fallback checked 24 recent runs, using 27 total requests including the simulated org call. `truncated: true` discloses that the latest-100 workflow listing has further pages; job reading itself stayed below the 30-run limit. The optional second repository is not queried. The capture script is `tests/dashboard-truth/homelab-source.py`.

Validation: 6 targeted tests pass; full suite 52/53 passes with the existing missing cascade-card marker failure confirmed on the unchanged branch. `git diff --check` passes. No live service changes or deployment.
