"""Evaluate the auditor on the 32 public samples plus participant-built invariance cases.

Usage:  python code/evaluation/main.py     -> prints metrics, writes code/evaluation/results.json
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import data  # noqa: E402
import main as runner  # noqa: E402
from evaluation import score  # noqa: E402

# Custom cases are derived from public samples by a change whose correct effect is known in advance.
TEXT_NON_DELETION = ("Please update the phone number on my profile. I want to keep using the account, "
                     "so please do not delete anything.")
TEXT_SPANISH = ("Solicito la eliminación definitiva de mi cuenta y de todos los datos personales asociados "
                "a esta dirección de correo. Por favor confirmen cuando termine la revisión.")


def custom_cases(samples, gold, identities):
    """[(name, case, expectation)]; expectation is ('status', value) or ('same_as', sample_case_id)."""
    out = []
    by_id = {c["case_id"]: c for c in samples}
    first = sorted(gold, key=lambda c: int(c.split("_")[1]))
    # 1. same case, request text turned into a correction request -> must not run a deletion
    c = dict(by_id[first[0]], case_id="custom_1", derived_from=first[0], request_text=TEXT_NON_DELETION)
    out.append(("non-deletion text on a deletion case", c, ("status", "NOT_APPLICABLE")))
    # 2. same case written in Spanish -> identical answer to the English sample
    c = dict(by_id[first[1]], case_id="custom_2", derived_from=first[1], request_text=TEXT_SPANISH)
    out.append(("multilingual deletion request", c, ("same_as", first[1])))
    # 3. request sent from the subject's historical email -> identical answer to the current-email sample
    for cid in first:
        uid = identities.resolve(by_id[cid]["email"])
        old = [h["old_value"] for h in identities.history.get(uid, []) if h["field_name"] == "email"
               and h["old_value"].lower() != by_id[cid]["email"].lower()]
        if uid and old:
            c = dict(by_id[cid], case_id="custom_3", derived_from=cid, email=old[0])
            out.append(("historical email resolves to same subject", c, ("same_as", cid)))
            break
    # 4. unknown email -> no subject to audit
    c = dict(by_id[first[2]], case_id="custom_4", derived_from=first[2], email="nobody.known@example.invalid")
    out.append(("unknown requester", c, ("status", "NOT_APPLICABLE")))
    return out


def no_future_evidence(results, cases, manifest):
    """Every cited file must be captured at or before the case's audit_at."""
    cap = {m["file_id"]: data.ts(m["captured_at"]) for m in manifest}
    bad = []
    for c in cases:
        for s in results[c["case_id"]][1]:
            e = s["evidence_id"]
            if e in cap and cap[e] > data.ts(c["audit_at"]):
                bad.append((c["case_id"], e))
    return bad


def main():
    samples = data.read("sample_deletion_requests.csv")
    evals = data.read("deletion_requests.csv")
    gold = score.load_gold(data.read("sample_output.csv"))
    auditor = runner.build_auditor([c["request_text"] for c in samples + evals]
                                   + [TEXT_NON_DELETION, TEXT_SPANISH])
    results = auditor.run(samples, samples + evals)
    metrics, diffs = score.score({k: (v[0], v[1]) for k, v in results.items()}, gold)

    customs = []
    for name, case, (kind, want) in custom_cases(samples, gold, auditor.identities):
        # replace the sample it was derived from so the workflow-assignment pool stays one-subject-per-case
        pool = [case if c["case_id"] == case["derived_from"] else c for c in samples + evals]
        got = auditor.run([case], pool)[case["case_id"]]
        if kind == "status":
            ok = got[0] == want
        else:
            ok = (got[0], got[1]) == gold[want]
        customs.append({"name": name, "case": case["case_id"], "expect": f"{kind}={want}", "got": got[0],
                        "pass": ok})

    future = no_future_evidence(results, samples, data.read("evidence_files.csv"))
    out = {"metrics": metrics, "diffs": diffs, "custom": customs, "future_evidence_violations": future,
           "intent_disagreements": [(t[:80], m["request_type"], kw) for t, m, kw in auditor.intent.disagreements]}
    json.dump(out, open(os.path.join(HERE, "results.json"), "w", encoding="utf-8"), indent=1, default=str)
    print(json.dumps(metrics, indent=1))
    for c in customs:
        print("custom", c)
    for k, v in diffs.items():
        print(k, v)
    print("future evidence violations:", future)


if __name__ == "__main__":
    main()
