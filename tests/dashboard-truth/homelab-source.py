"""Live scratch evidence; only the fallback-triggering org 403 is simulated."""
import json, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, Mock
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import queue_router as q
out=ROOT / 'evidence/homelab-source'
out.mkdir(parents=True, exist_ok=True)
real_get=q.requests.get
calls=[]
def denied_org(url, **kwargs):
    calls.append(url)
    if '/orgs/armbrain-io/actions/runners' in url:
        return Mock(status_code=403)
    return real_get(url, **kwargs)
with patch.object(q.requests,'get',side_effect=denied_org):
    history=q.app.test_client().get('/api/local_runners?fresh=1').get_json()
(out/'job-history.json').write_text(json.dumps(history,indent=2)+'\n')
print({k:v for k,v in history.items() if k not in ('hosts','runners')}, flush=True)
assert history['available'] and history['source']=='job_history'
def gh(path):
    return json.loads(subprocess.check_output(['gh','api','--paginate','--slurp',path],text=True))
# Independently read recent runs of every status: rerun/queued workflows can
# still expose jobs with status=in_progress.
recent=json.loads(subprocess.check_output(['gh','api','repos/armbrain-io/armbrain/actions/workflows/255384592/runs?per_page=100'],text=True))
run_ids=[r['id'] for r in recent['workflow_runs'] if datetime.fromisoformat(r['updated_at'].replace('Z','+00:00')) >= datetime.fromisoformat(history['since'])]
def busy_jobs(run_id):
    names=set()
    for jobs in gh(f"repos/armbrain-io/armbrain/actions/runs/{run_id}/jobs?per_page=100"):
        for job in jobs['jobs']:
            name=job.get('runner_name') or ''
            if job['status']=='in_progress' and name and not name.startswith('runs-on--') and not any('runs-on=' in l for l in job.get('labels',[])):
                names.add(name)
    return names
with ThreadPoolExecutor(max_workers=6) as pool:
    busy=set().union(*pool.map(busy_jobs,run_ids))
truth=dict(captured_at=datetime.now(timezone.utc).isoformat(),method='Fresh independent gh api recent workflow runs of every status and paginated jobs filtered to in_progress; no production helpers',fallback_trigger='Only org inventory HTTP 403 simulated; all workflow/job requests live with normal service token',run_ids=run_ids,busy=len(busy),runner_names=sorted(busy),api_busy=history['busy'],busy_matches=len(busy)==history['busy'],api_calls=len(calls))
(out/'truth.json').write_text(json.dumps(truth,indent=2)+'\n')
print(truth,flush=True)
q.local_runners_cache.update(data=None,ts=0)
live=q.get_local_runners(True)
(out/'live.json').write_text(json.dumps(live,indent=2)+'\n')
pages=gh('orgs/armbrain-io/actions/runners?per_page=100')
runners=[r for page in pages for r in page['runners'] if not r['name'].startswith('runs-on--') and not any('runs-on=' in l['name'] for l in r['labels'])]
truth['inventory_online']=sum(r['status']=='online' for r in runners)
truth['api_inventory_online']=live['online']
truth['inventory_matches']=truth['inventory_online']==live['online']
(out/'truth.json').write_text(json.dumps(truth,indent=2)+'\n')
assert truth['busy_matches'] and truth['inventory_matches'],truth
