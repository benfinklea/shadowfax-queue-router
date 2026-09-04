# Fleet Monitor truth inventory

The suite treats the dashboard as a claim and reads the underlying system again through a separate path. It never calls the power, reboot, swap-clear, or reset controls. CI scope is exactly `armbrain-io/armbrain` workflow runs created in the last 48 hours: `status=queued` and `status=in_progress`; stale queued runs older than 48 hours are shown separately as orphans and excluded from the queue-depth claim. GitHub verification uses one GraphQL request plus REST core requests, never more than 3 Search API requests and fewer than 15 core requests per run.

## API signals

| Signal(s) | Dashboard source | Independent verification |
|---|---|---|
| `GET /api/health` (`status`, `service`) | Flask process | HTTP 200, JSON schema, exact service/status |
| `GET /api/status` (`targets`, `recent_jobs`) | fresh SSH target probes plus SQLite | HTTP/schema; TCP/SSH hostname, `nvidia-smi`, serving props, and direct SQLite |
| `GET /api/fleet` (six named nodes) | SSH CPU/RAM/temp probe | HTTP/schema; ping, TCP/22, SSH hostname; dashboard `up` follows the primary SSH path |
| `GET /api/pipeline` | cached GitHub reads | HTTP/schema; direct `gh api` GraphQL and Actions REST; `generated_at` younger than 10 minutes |
| `GET /api/ci_queue` | Actions workflow runs for `armbrain-io/armbrain`, last 48h; runner/job detail is ancillary | direct Actions REST with the identical repo/window/status scope, ±1 churn |
| `GET /api/runson` | read-only AWS CLI, profile `armbrain`; GitHub Actions variable with completed-test-job fallback | schema; live data fields, or explicit actionable `credentials` / `aws login` state; `gate_shards` agrees with direct `gh variable list -R armbrain-io/armbrain` |
| `GET /api/model_serving` | 60-second llama.cpp counter samples plus gateway spend log | direct serving `/metrics`; route presence for gateway-derived boxes; sanity/range checks |
| `GET /api/fleet_stats` | direct process counts and GitHub org runners | total equals per-box sum; runner fleet aggregate equals per-box aggregate |
| `GET /api/model_routes` | gateway `/v1/models`, config topology, resident-model endpoints | authenticated direct gateway `/v1/models` exact per-route live state |
| `GET /api/vram-processes` | `nvidia-smi` compute-app list | JSON schema plus independent `nvidia-smi` VRAM totals |
| `GET /api/logs` | latest 20 SQLite jobs | direct SQLite row count/order window |
| `GET /api/history?range=hour` | SQLite `metrics_history` | direct row counts and all spark-series fields per point |
| `GET /api/energy` | integration of SQLite watt samples | fleet totals equal direct sum of metered machine totals for day/week/month |
| `POST/DELETE /api/reset_stats` | local reset marker | route/method contract checked statically; not invoked by read-only suite |
| `POST /api/fleet_power` | SSH reboot/poweroff or WoL | route/method contract checked statically; not invoked by read-only suite |
| `POST /api/power_limit` | remote `nvidia-smi -pl` | route/method contract checked statically; not invoked by read-only suite |
| `POST /api/clear_swap` | remote swap cycle | route/method contract checked statically; not invoked by read-only suite |
| `POST /api/power` | remote reboot/shutdown | route/method contract checked statically; not invoked by read-only suite |

## Rendered signals

| Card/value | Source of truth | Independent verification |
|---|---|---|
| Shipping: issues open, PRs open | GitHub repository search | one direct GraphQL query, ±1 churn |
| Shipping: CI queued/running | Actions run count (queued) and active job count (running, from first 12 runs) | direct REST counts, ±1 churn |
| Shipping: merged today, merged in last 60m | PR merge timestamps; day boundary is Central Time | direct GraphQL counts, ±1 churn |
| Shipping: merged-rate box colour | `/api/pipeline.merged_last_hour` integer count | pure renderer fixtures assert 0=pulsing red, 1=red, 2=yellow, and 3/7=green; live box binding uses `merged_last_hour` |
| Shipping: last merge time | newest `merged_at` among recently closed PRs | direct pulls REST exact timestamp |
| Shipping: merged seven-day spark | seven Central-Time daily merge buckets | seven-integer shape plus GitHub source availability |
| Shipping: deployed today, failures, in flight | production `push`/`workflow_dispatch` runs of `gateway-deploy.yml` | direct Actions workflow runs and nonnegative typed counts |
| Shipping: deploy seven-day spark | successful production deploy workflow runs | seven-integer shape plus GitHub source availability |
| Shipping: last deploy time/SHA | latest successful `deploy` job on main | direct runs and jobs REST, exact SHA/time |
| Shipping: updated chip/degraded state | `/api/pipeline.generated_at` and auth state | timestamp must parse and be under 10 minutes old |
| CI card: queued/running workflow runs, active runners, orphans | scoped Actions runs; runner names are ancillary detail from jobs on the first 12 in-progress runs | direct queued/in-progress Actions run counts, bracketed at ±1; schema distinguishes unavailable from zero |
| Fleet tiles: per-box ONLINE/OFFLINE/MISMATCH | SSH metric probe and returned hostname | ping + TCP/22 + SSH hostname. When ping works but SSH does not, output says `primary_path=FAIL; box alive via alternate path, gandalf->box path broken` |
| Fleet tiles: CPU, RAM, temperature | remote `/proc`, `free`, hwmon | fresh SSH instrument and schema; identity guard prevents wrong-box values |
| Core `n/4` and Reserve `n/5` | three target cards + Shadowfax; five Shire/reserve tiles | recomputed from fresh primary-path probes |
| Target card status | fresh SSH primary path | ping/TCP/SSH hostname comparison |
| GPU utilization dial | `nvidia-smi` sampled by dashboard | fresh remote `nvidia-smi`, 20-point sampling tolerance |
| GPU temperature dial (all except Gandalf/Frodo/Aragorn) | `nvidia-smi` | fresh remote `nvidia-smi`, ±5 C |
| GPU power dial/limit and GPU identity/count | `nvidia-smi` and configured ceiling | fresh `nvidia-smi`, sampling tolerance and required schema |
| Tokens/sec dial (Gandalf/Frodo/Aragorn) | 60-second deltas of llama.cpp lifetime token/serving-second counters; Aragorn sums live :11434/:11435 instances | direct serving `/metrics` counters plus per-card DOM marker; unavailable data must stay dim |
| VRAM used/total bar and process tooltip | `nvidia-smi` memory and compute apps | fresh `nvidia-smi`, ±5% (plus 0.2 GiB floor) |
| CPU, RAM, swap, disk, disk I/O, network I/O | SSH `/proc`, `free`, `df`, counters | API required values/ranges and fresh host identity; history columns verify persistence |
| Peak-hold markers and max utilization today | SQLite metrics since reset/midnight | direct history presence and typed peaks |
| Loaded model identity | serving `/props`; Gandalf llama-swap `/running` | exact normalized model filename; no alias/fuzzy identity match |
| Pippin loaded-model memory | resident `llama-server` RSS from guarded SSH process probe | headless DOM after two 12-second refresh intervals must show `RSS <number> GB` or dimmed `RSS n/a`, never `measuring…` |
| Tokens/sec now, peak, average | direct llama.cpp `/metrics` counters or gateway spend log | live counters/source continuity and numeric sanity; current display is a 60-second delta |
| Tokens/sec integer presentation | TPS now, today peak, average, and gauge limit | every rendered TPS readout uses `Math.round`, including peak and gauge limit |
| Requests served 1h/24h/7d; serving minutes | counter burst estimates or gateway spend log | typed model-serving schema and direct source continuity |
| Model route chips: live/loaded/idle/missing, box, model | gateway `/v1/models`, live config, serving resident set | exact live route membership from gateway; identity tests cover resident model |
| Route-health summary and dots | route rows | recompute live/loaded/missing counts from `/api/model_routes` rows |
| Fleet agents and CI runners | per-box process list and GitHub runners | totals must equal per-box aggregates; unavailable is explicit, never silent zero |
| History sparks (GPU, CPU, temp, VRAM, RAM, swap, queue) | `metrics_history` by selected range | direct SQLite hour row counts and each plotted column present |
| Energy by machine and fleet (day/week/month) | trapezoidal integration of GPU watt history and PEC rates | fleet aggregate equals sum of per-machine displayed results |
| RunsOn live runners/type/age, jobs today, trial days/date, credits | CloudFormation, EC2, DynamoDB, Free Tier APIs | required live schema, or loud exact expired-credentials message (accepted pass state) |
| RunsOn shard-state notice and card glow | `RUNSON_GATE_SHARDS` Actions variable (`on`/`off`), with explicit workflow-job fallback or unknown error | direct `gh variable list`; headless DOM class and resolved notice match the API state |
| Static inventory table and API endpoint card | HTML constants | page HTTP 200 and required container IDs |
| All cards and polling/render functions | inline HTML/JavaScript | required containers/functions present and JavaScript passes `node --check` |
| Control buttons (wake/reboot/shutdown/power limit/clear swap/reset) | declared Flask routes and JS handlers | static contract only; hourly checker remains read-only |

The inventory contains 19 API route contracts (14 read-only request forms plus 5 mutating contracts) and 59 rendered signal groups, for **78 enumerated signals/groups**. Per-box/per-route expansion produces one result line for each concrete live signal.
