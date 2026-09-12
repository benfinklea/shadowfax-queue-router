"""Ben, 2026-09-12: "the CI/CD pipeline has disappeared" after each restart.
The last good pipeline snapshot must survive a process restart: it is written
to disk on every good refresh and restored at import, and the fast read path
serves it (stale-while-revalidating) instead of the cold "warming_up" blank.
Runs without a server: python3 tests/dashboard-truth/test_pipeline_last_good_persist.py
"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
tmpdir = tempfile.mkdtemp()
path = os.path.join(tmpdir, "state", "pipeline-last-good.json")
os.environ["PIPELINE_LAST_GOOD_PATH"] = path

def load_module():
    spec = importlib.util.spec_from_file_location("qr_persist_test", ROOT / "queue_router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# 1. Cold boot, nothing on disk: the fast path reports warming_up, not a snapshot.
qr = load_module()
cold = qr.get_pipeline_status_fast(repo="armbrain-io/armbrain", default_branch="main")
assert cold.get("available") is False and cold.get("warming_up") is True, cold

# 2. A good snapshot lands and is persisted.
snap = {"available": True, "degraded": False, "repo": "armbrain-io/armbrain",
        "issues_open": 188, "prs_open": 52, "generated_at": "2026-09-12T05:00:00+00:00"}
qr.pipeline_last_good["armbrain-io/armbrain"] = dict(snap)
assert qr._persist_last_good(qr.pipeline_last_good) is True
assert os.path.exists(path), path
assert json.load(open(path))["armbrain-io/armbrain"]["issues_open"] == 188

# 3. "Restart": a fresh import restores it and the fast path serves it as
#    stale-while-revalidating with the ORIGINAL generated_at - never warming_up,
#    never a blank strip.
qr2 = load_module()
assert qr2.pipeline_last_good.get("armbrain-io/armbrain", {}).get("restored_from_disk") is True, qr2.pipeline_last_good.keys()
warm = qr2.get_pipeline_status_fast(repo="armbrain-io/armbrain", default_branch="main")
assert warm.get("available") is True and warm.get("issues_open") == 188, warm
assert warm.get("stale_while_revalidating") is True and warm.get("generated_at") == snap["generated_at"], warm
assert not warm.get("warming_up"), warm

# 4. A corrupt file never breaks boot - it is treated as "nothing on disk".
with open(path, "w") as f:
    f.write("{not json")
qr3 = load_module()
assert qr3.pipeline_last_good.get("armbrain-io/armbrain") is None
print("OK pipeline last-good persists across restart and survives a corrupt file")
