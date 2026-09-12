"""STAGE-HOVER guard: the PRS OPEN hover breakdown.

Ben, 2026-09-12: hovering a stage must show what is inside it. Two properties
have to hold or the tooltip is worse than nothing - it would be a confident
wrong number on the tile Ben reads most:

  1. The buckets SUM to the total. They are first-match over an ordered list,
     so every open PR lands in exactly one.
  2. Only NON-ZERO buckets render, in the server's order, under a header that
     is the payload's own total (never the tile's number, which can disagree
     with the buckets if the payload is mid-refresh).

Deterministic and offline: fixture-driven, no GitHub calls, no browser. The
screenshot-capturability half of the feature is proved by stage-hover-dom.py.

    python3 stage-hover.py            # fixture
    python3 stage-hover.py prs.json   # also check a real `gh pr list --json` dump
"""
import json
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
import queue_router as qr

fails = []
def check(ok, label, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ((" - " + detail) if detail else ""))
    if not ok:
        fails.append(label + ((" - " + detail) if detail else ""))

# ── fixture: one PR per bucket, plus PRs that MUST be claimed by an earlier
# bucket than the one they also qualify for (that is what first-match means).
def pr(n, draft=False, labels=(), mergeable="MERGEABLE", state="CLEAN", review="APPROVED"):
    return {"number": n, "title": f"pr{n}", "isDraft": draft,
            "labels": [{"name": l} for l in labels], "mergeable": mergeable,
            "mergeStateStatus": state, "reviewDecision": review}

FIXTURE = [
    pr(1, draft=True),                                                    # drafts
    pr(2, draft=True, mergeable="CONFLICTING", state="DIRTY"),            # drafts (not conflicted)
    pr(3, draft=True, labels=["hold"]),                                   # drafts (not human-gated)
    pr(4, labels=["blocked-on-ben"]),                                     # human-gated
    pr(5, labels=["HOLD"]),                                               # human-gated (case-insensitive)
    pr(6, labels=["needs-human"], mergeable="CONFLICTING", state="DIRTY"),# human-gated (not conflicted)
    pr(7, labels=["do-not-merge"], review="REVIEW_REQUIRED"),             # human-gated (not unreviewed)
    pr(8, mergeable="CONFLICTING", state="DIRTY", review="REVIEW_REQUIRED"),  # conflicted (not unreviewed)
    pr(9, review="REVIEW_REQUIRED"),                                      # unreviewed
    pr(10, review="CHANGES_REQUESTED"),                                   # unreviewed
    pr(11, review=None),                                                  # unreviewed (no decision yet)
    pr(12, state="UNSTABLE"),                                             # approved, checks failing
    pr(13, state="BLOCKED"),                                              # approved, checks failing
    pr(14, state="CLEAN"),                                                # ready to merge
    pr(15, labels=["needs-repair"], state="CLEAN"),                       # ready (needs-repair is NOT in Ben's gate set)
]
EXPECTED = {"drafts": 3, "human-gated": 4, "conflicted": 1, "unreviewed": 3,
            "approved, checks failing": 2, "ready to merge": 2}

bd = qr._pr_open_breakdown(FIXTURE)
got = {b["label"]: b["count"] for b in bd["buckets"]}
check(bd["total"] == len(FIXTURE), "fixture total equals the PR count", f"{bd['total']} vs {len(FIXTURE)}")
check(sum(got.values()) == bd["total"], "fixture buckets sum to the total",
      f"{sum(got.values())} vs {bd['total']}")
check(got == EXPECTED, "fixture first-match bucketing", f"got {got}")
check([b["label"] for b in bd["buckets"]] == list(qr.PR_OPEN_BUCKET_ORDER),
      "buckets render in PR_OPEN_BUCKET_ORDER")
check(bd["noun"] == "PRs", "header noun is PRs")

# ── an empty repo must not claim anything
empty = qr._pr_open_breakdown([])
check(empty["total"] == 0 and sum(b["count"] for b in empty["buckets"]) == 0,
      "empty PR list yields an all-zero breakdown")

# ── MUTATION: break each rule on purpose; a named assertion above must catch it.
# A mutant that dies by traceback is a weak kill and is reported as a failure.
src = Path(qr.__file__).read_text()
fn_start = src.index("def _pr_open_breakdown(")
fn_end = src.index("\ndef ", fn_start + 1)
fn_src = src[fn_start:fn_end]
prelude = (f"PR_OPEN_HUMAN_GATE_LABELS = {qr.PR_OPEN_HUMAN_GATE_LABELS!r}\n"
           f"PR_OPEN_BUCKET_ORDER = {tuple(qr.PR_OPEN_BUCKET_ORDER)!r}\n")

MUTANTS = [
    ("drafts checked after human-gated", 'if pr.get("isDraft"):', 'if False:', "drafts"),
    ("approval test inverted", '!= "APPROVED"', '== "APPROVED"', "unreviewed"),
    ("BLOCKED dropped from failing checks", '("UNSTABLE", "BLOCKED")', '("UNSTABLE",)',
     "approved, checks failing"),
    ("label match made case-sensitive", '(label.get("name") or "").lower()',
     '(label.get("name") or "")', "human-gated"),
    ("a bucket stops counting", "counts[name] += 1",
     'counts[name] += 0 if name == "conflicted" else 1', None),
]
for label, find, repl, bucket in MUTANTS:
    if find not in fn_src:
        check(False, f"mutant anchor present: {label}", f"anchor {find!r} not in source")
        continue
    ns = {}
    try:
        exec(compile(prelude + fn_src.replace(find, repl), "<mutant>", "exec"), ns)
        m = ns["_pr_open_breakdown"](FIXTURE)
        mgot = {b["label"]: b["count"] for b in m["buckets"]}
    except Exception as e:
        check(False, f"mutant killed by assertion: {label}",
              f"died by {type(e).__name__} traceback - weak kill")
        continue
    if bucket is None:
        killed = sum(mgot.values()) != m["total"]
        detail = f"sum {sum(mgot.values())} vs total {m['total']}"
    else:
        killed = mgot.get(bucket) != EXPECTED[bucket]
        detail = f"{bucket} {EXPECTED[bucket]} -> {mgot.get(bucket)}"
    check(killed, f"mutant killed: {label}", detail)

# ── the client renderer, taken from the SERVED page (not a copy in this test).
page = qr.app.test_client().get("/").get_data(as_text=True)
js = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
def grab(name, decl="function"):
    m = re.search(r"(?:^|\n)(?:let |function )" + name + r"\b", js)
    if not m:
        return None
    if decl == "let":
        st = js.index("let", m.start())
        return js[st:js.index("\n", st) + 1]
    i = js.index("{", m.start()); depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[m.start():j + 1]
    return None

pieces = [grab("shipBreakdowns", "let"), grab("shipEscape"),
          grab("shipSetBreakdowns"), grab("shipBreakdownText")]
if not all(pieces):
    check(False, "served page exposes the breakdown renderer", "one or more functions not found")
else:
    harness = "\n".join(pieces) + """
const out = [];
shipSetBreakdowns({prs_open_breakdown: BD});
out.push(shipBreakdownText('prs open', BD.total));
out.push(shipBreakdownText('in queue', 3));
shipSetBreakdowns({});
out.push(shipBreakdownText('prs open', 99));
shipSetBreakdowns({prs_open_breakdown: {noun:'PRs', total:0, buckets:[{label:'drafts',count:0}]}});
out.push(shipBreakdownText('prs open', 0));
console.log(JSON.stringify(out));
"""
    try:
        cp = subprocess.run(["node", "-e", f"const BD={json.dumps(bd)};\n{harness}"],
                            capture_output=True, text=True, timeout=60, check=True)
        rendered, other_stage, no_payload, all_zero = json.loads(cp.stdout.strip().splitlines()[-1])
    except FileNotFoundError:
        print("SKIP client renderer checks - node is not installed (0 compared)")
        rendered = None
    except Exception as e:
        check(False, "client renderer executes", str(e)[:200])
        rendered = None
    if rendered is not None:
        lines = rendered.split("\n")
        shown = [b for b in bd["buckets"] if b["count"] > 0]
        check(lines[0] == f"{bd['total']} PRs", "tooltip header is total + noun", repr(lines[0]))
        check(len(lines) == len(shown) + 1, "one line per non-zero bucket, plus the header",
              f"{len(lines)} lines for {len(shown)} non-zero buckets")
        check(not any(l.startswith("0 ") for l in lines[1:]), "no zero bucket is rendered")
        check([l.split(" ", 1)[1] for l in lines[1:]] == [b["label"] for b in shown],
              "rendered order matches the server order")
        s = sum(int(l.split(" ")[0]) for l in lines[1:])
        check(s == bd["total"], "rendered lines sum to the header total", f"{s} vs {bd['total']}")
        check(other_stage == "", "a stage with no breakdown renders nothing")
        check(no_payload == "", "a payload with no breakdown renders nothing")
        check(all_zero == "", "an all-zero breakdown renders nothing (not a bare total)")

# ── optional: a real `gh pr list --json ...` dump, if one was handed to us.
if len(sys.argv) > 1:
    live = json.loads(Path(sys.argv[1]).read_text())
    lb = qr._pr_open_breakdown(live)
    s = sum(b["count"] for b in lb["buckets"])
    check(lb["total"] == len(live) == s, f"live dump of {len(live)} PRs sums",
          f"total {lb['total']}, sum {s}, rows {len(live)}")
    for b in lb["buckets"]:
        print(f"      {b['count']:>4}  {b['label']}")

print()
if fails:
    print(f"FAIL - {len(fails)} check(s) failed")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("PASS - buckets sum, first-match order holds, 5 mutants killed by assertion, "
      "renderer shows only non-zero buckets")
