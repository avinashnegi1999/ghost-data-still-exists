"""End-to-end audit for a list of requests."""
import json
from collections import defaultdict

import data
import engine
import evidence
import linker


class Auditor:
    def __init__(self, media_rows=None, intent=None):
        self.identities = data.Identities()
        self.activity = data.read("system_activity.csv")
        self.obs, self.scans, self.files = evidence.load_all(data.DATASET, media_rows)
        self.by_rid = defaultdict(list)
        for o in self.obs:
            self.by_rid[o["rid"]].append(o)
        self.catalog = linker.catalog_links(self.activity)
        self.intent = intent  # callable(case) -> bool authorizes deletion

    def run(self, cases, context_cases=None):
        """Audit `cases`; `context_cases` (superset) share the workflow-assignment pool."""
        pool = context_cases or cases
        subjects = {c["case_id"]: self.identities.resolve(c["email"]) for c in pool}
        fixed, ties = linker.assign_workflow_resources(pool, subjects, self.obs, self.identities, self.activity)
        by_id = {c["case_id"]: c for c in pool}
        accounts_only = lambda wf: {r: s for r, s in wf.items() if s == "account_service"}
        prelim = {}
        for cid in set(fixed) | {c for cids in ties.values() for c in cids}:
            if cid in by_id:
                prelim[cid] = self.audit(by_id[cid], subjects[cid], accounts_only(fixed.get(cid, {})))[0]
        # an anonymized/redacted leftover is evidence the subject's workflow finished; give tied
        # workflow records to subjects whose audit is otherwise clean before those with open findings
        prefer = lambda cid, system: 0 if system == "account_service" or prelim.get(cid) == "COMPLETE" else 1
        extra = linker.resolve_ties(ties, subjects, prefer)
        out = {}
        for c in cases:
            wf = dict(fixed.get(c["case_id"], {}))
            wf.update(extra.get(c["case_id"], {}))
            if prelim.get(c["case_id"]) != "COMPLETE":
                # workflow-only records with no identifier are verification-only facts; they are
                # attributed only to subjects whose workflow otherwise finished cleanly
                wf = accounts_only(wf)
            out[c["case_id"]] = self.audit(c, subjects[c["case_id"]], wf)
        return out

    def resources(self, case, uid, workflow):
        res = linker.evidence_resources(self.obs, self.identities, uid, case)
        lo = data.ts(case["requested_at"]) - linker.LINK_WINDOW_BEFORE
        hi = data.ts(case["audit_at"]) + linker.LINK_WINDOW_AFTER
        for rid, (u, _aid, t) in self.catalog.items():
            if u == uid and lo <= data.ts(t) <= hi:
                res[rid] = evidence.system_of(rid)
        for rid, system in workflow.items():
            res.setdefault(rid, system)
        # one account per subject: keep the profile observed closest to this request
        accounts = [r for r, s in res.items() if s == "account_service"]
        if len(accounts) > 1:
            req = data.ts(case["requested_at"])
            def dist(r):
                ts_ = [abs((data.ts(o["t"]) - req).total_seconds()) for o in self.by_rid.get(r, [])]
                return min(ts_) if ts_ else 1e12
            keep = min(accounts, key=lambda r: (r not in workflow, dist(r)))
            for r in accounts:
                if r != keep:
                    res.pop(r)
        return res

    def audit(self, case, uid, workflow):
        if self.intent and not self.intent(case):
            return "NOT_APPLICABLE", [], {}
        if not uid:
            return "NOT_APPLICABLE", [], {}
        audit_at = data.ts(case["audit_at"])
        res = self.resources(case, uid, workflow)
        assessments = [engine.assess(rid, system, self.by_rid.get(rid, []), self.scans, audit_at)
                       for rid, system in sorted(res.items())]
        status, steps = engine.build_steps(assessments)
        return status, steps, {"subject": uid, "resources": res, "assessments": assessments}


def dumps(steps):
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
