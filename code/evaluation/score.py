"""Scoring helpers: compare predicted rows against the public sample answers."""
import json
from difflib import SequenceMatcher

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def key(step):
    return tuple(step[f] for f in FIELDS)


def score(pred, gold):
    """pred/gold: {case_id: (status, steps)} -> metrics dict + per-case diffs."""
    m = dict(cases=0, status=0, exact=0, gold_steps=0, pred_steps=0, step_tp=0, seq=0.0)
    field_hits = {f: 0 for f in FIELDS}
    diffs = {}
    for cid, (gs, gsteps) in gold.items():
        ps, psteps = pred.get(cid, ("MISSING", []))
        m["cases"] += 1
        m["status"] += ps == gs
        gk, pk = [key(s) for s in gsteps], [key(s) for s in psteps]
        m["exact"] += gk == pk and ps == gs
        m["gold_steps"] += len(gk)
        m["pred_steps"] += len(pk)
        remaining = list(pk)
        for k in gk:
            if k in remaining:
                remaining.remove(k)
                m["step_tp"] += 1
        # field accuracy on position-aligned steps
        for i, g in enumerate(gsteps):
            if i < len(psteps):
                for f in FIELDS:
                    field_hits[f] += psteps[i][f] == g[f]
        m["seq"] += SequenceMatcher(None, gk, pk).ratio() if (gk or pk) else 1.0
        if gk != pk or ps != gs:
            diffs[cid] = dict(gold_status=gs, pred_status=ps, missing=[k for k in gk if k not in pk],
                              extra=[k for k in pk if k not in gk])
    n = max(m["cases"], 1)
    p = m["step_tp"] / max(m["pred_steps"], 1)
    r = m["step_tp"] / max(m["gold_steps"], 1)
    metrics = {
        "cases": m["cases"],
        "status_accuracy": m["status"] / n,
        "exact_case_match": m["exact"] / n,
        "step_precision": p,
        "step_recall": r,
        "step_f1": 2 * p * r / (p + r) if p + r else 0.0,
        "sequence_similarity": m["seq"] / n,
        "field_accuracy": {f: field_hits[f] / max(m["gold_steps"], 1) for f in FIELDS},
    }
    return metrics, diffs


def load_gold(rows):
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in rows}
