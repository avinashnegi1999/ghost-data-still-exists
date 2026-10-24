"""Connect resources to a case's data subject.

Three sources, strongest first:
1. evidence contents: a record (or the lookup that found it) carries one of the subject's current or
   historical identifiers (email, phone, email hash, internal user reference);
2. data-catalog LINK_RESOURCE_IDENTITY activity rows;
3. the deletion workflow's own actions: the worker touches each subject's resources shortly before
   the workflow reports. Those rows do not name the subject, so they are assigned by elimination
   across every case that reported in the same window (see assign_workflow_resources).
"""
import re
from collections import defaultdict
from datetime import timedelta

from data import ts

LINK_WINDOW_BEFORE = timedelta(days=45)
LINK_WINDOW_AFTER = timedelta(days=45)
WORKFLOW_BEFORE = timedelta(hours=6)
WORKFLOW_AFTER = timedelta(minutes=15)
TIGHT = timedelta(minutes=20)
POLICY_SYSTEMS = {"account_service", "analytics_platform", "identity_service", "support_platform",
                  "integration_service", "backup_service", "search_service", "export_service"}


def subject_values(identities, uid):
    return {v for v in identities.ids[uid] if not v.startswith("name:")}


def obs_owner_index(obs, identities):
    """rid -> set(user_id) for every user any observation of that resource identifies."""
    owners = defaultdict(set)
    for o in obs:
        for v in o["refs"] | o["direct"] | o["lookup"]:
            if v.startswith("name:"):
                continue
            for u in identities.by_value.get(v, ()):
                owners[o["rid"]].add(u)
    return owners


def catalog_links(activity):
    """LINK_RESOURCE_IDENTITY rows -> {rid: user_id}."""
    out = {}
    for a in activity:
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            m = re.search(r"\b(user_\d+)\b", a["details"])
            if m:
                out[a["resource_id"]] = (m.group(1), a["activity_id"], a["event_time"])
    return out


def evidence_resources(obs, identities, uid, case):
    lo = ts(case["requested_at"]) - LINK_WINDOW_BEFORE
    hi = ts(case["audit_at"]) + LINK_WINDOW_AFTER
    vals = subject_values(identities, uid)
    found = {}
    for o in obs:
        if (o["refs"] | o["direct"] | o["lookup"]) & vals and lo <= ts(o["t"]) <= hi:
            found[o["rid"]] = o["system"]
    return found


def assign_workflow_resources(cases, subjects, obs, identities, activity):
    """Global elimination: {case_id: {rid: system}} for workflow-touched resources.

    For each case and system, candidates are deletion-worker actions in the window just before the
    workflow reported. Candidates already tied by evidence to another person are dropped; candidates tied
    to this subject are taken. Remaining candidates must co-occur with the subject's other resources in the
    same evidence file (the snapshot that captured this case) unless the system is the account store, which
    every subject has. Singletons are fixed and removed from every other case's domain until a fixpoint.
    """
    owners = obs_owner_index(obs, identities)
    files_of_rid = defaultdict(set)
    for o in obs:
        files_of_rid[o["rid"]].add(o["file"])
    worker = [a for a in activity if a["source_system"] == "deletion_worker"
              and a["target_system"] in POLICY_SYSTEMS]

    domains = {}
    case_files = {}
    own_systems = {}
    reported = {}
    for c in cases:
        uid = subjects.get(c["case_id"])
        if not uid:
            continue
        own = evidence_resources(obs, identities, uid, c)
        case_files[c["case_id"]] = {f for r in own for f in files_of_rid[r]}
        own_systems[c["case_id"]] = set(own.values())
        rep = ts(c["reported_at"])
        reported[c["case_id"]] = rep
        lo = max(ts(c["requested_at"]), rep - WORKFLOW_BEFORE)
        for a in worker:
            t = ts(a["event_time"])
            if lo <= t <= rep + WORKFLOW_AFTER:
                domains.setdefault((c["case_id"], a["target_system"]), {})[a["resource_id"]] = t

    fixed = defaultdict(dict)
    taken = {}
    pending = {}
    for (cid, system), cands in domains.items():
        uid = subjects[cid]
        if system in own_systems[cid] and system != "support_platform":
            continue  # evidence already links this subject's record in that system
        free = {r: t for r, t in cands.items() if not owners.get(r)}
        if not free:
            continue
        co = {r for r in free if files_of_rid[r] & case_files[cid]}
        if not co and system != "account_service":
            # hand-run workflows act minutes before reporting; accept a lone action in that tight window
            rep = reported[cid]
            near = {r: t for r, t in free.items() if rep - TIGHT <= t <= rep + WORKFLOW_AFTER}
            if len(near) != 1:
                continue
            free = near
        pending[(cid, system)] = (free, co)

    def live_of(key):
        free, co = pending[key]
        live = {r: t for r, t in free.items() if r not in taken}
        live_co = {r: t for r, t in live.items() if r in co}
        if live_co or key[1] != "account_service":
            return live_co if co else live
        return live

    changed = True
    while changed:
        changed = False
        for key in list(pending):
            live = live_of(key)
            if len(live) == 1:
                r = next(iter(live))
                fixed[key[0]][r] = key[1]
                taken[r] = key[0]
                del pending[key]
                changed = True
            elif not live:
                del pending[key]
                changed = True

    ties = defaultdict(list)
    for (cid, system) in pending:
        ties[(system, tuple(sorted(live_of((cid, system)))))].append(cid)
    return fixed, ties


def resolve_ties(ties, subjects, prefer):
    """Pair tied subjects with tied resources.

    prefer(case_id) -> sort key; lower keys are served first. With more subjects than resources this
    decides who gets one. ponytail: the pairing order itself is a guess (numeric order) because no
    evidence distinguishes the candidates; it is right about half the time for two-way ties.
    """
    num = lambda s: int(re.sub(r"\D", "", s) or 0)
    out = defaultdict(dict)
    taken = set()
    for (system, rids), cids in sorted(ties.items()):
        free = [r for r in sorted(rids, key=num) if r not in taken]
        chosen = sorted(cids, key=lambda c: (prefer(c, system), num(subjects[c])))[:len(free)]
        for cid, r in zip(sorted(chosen, key=lambda c: num(subjects[c])), free):
            out[cid][r] = system
            taken.add(r)
    return out
