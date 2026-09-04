# HOME LAB filter evidence

Captured 2026-09-04 around 22:29 UTC.

- `before.json`: unmodified live gandalf:5000/api/local_runners response: online 114, busy 64, idle 50; all 186 listed runners are cloud runners under the requested predicate.
- `after.json`: scratch Flask test-client GET /api/local_runners?fresh=1, using the normal CI installation-token provider and live GitHub requests: online 0, busy 0, idle 0, excluded_cloud 147, runners/hosts empty.
- `truth.json`: independent fresh `gh api --paginate --slurp 'repos/armbrain-io/armbrain/actions/runners?per_page=100'` read: two pages, 147 cloud runners, zero filtered runners. Filtered online equals the scratch endpoint; no cloud names are listed. Cloud inventory changed between the before and after reads.
- `live-filtered.png`: browser rendering of the actual after payload.
- `hosts-fixture.png`: controlled fixture proving the host expander shows northfarthing with 2 online / 1 busy / 1 idle and runner details. These are fixture counts, not live home-lab counts.

Validation: 3 new regression tests pass (Link pagination including short linked pages, both exclusion predicates, host grouping, offline counts, cache, empty results, partial failure and 403/404). Chromium checks pass for host counts, runner details, excluded count and live empty inventory. Full unittest discovery: 50 tests, 49 pass, one existing failure in test_home_contains_cascade_waterfall; its required literal `id=cascade-card` is also absent from origin/master at 6ee1c413fa1c4fcfe9a8a2613f6795964b5f79ec. git diff --check passes.

No deployment or changes to /workspace/monitoring/queue-router.
