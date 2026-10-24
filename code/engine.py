"""Deletion-audit reasoning: resource state at audit_at -> policy decision -> ordered steps."""
from collections import defaultdict
from datetime import timedelta

from data import ts

FIND_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REM_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VER_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
             "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
STATE = {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
         "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
         "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
         "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
         "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
         "VERIFY_RESTORE_BLOCKED": "BLOCKED"}

# policy treatment per category (mirrors dataset/deletion_rules.csv)
TREATMENT = {"USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
             "CACHE_ENTRY": "DELETE", "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE",
             "ANALYTICS_PROFILE": "ANONYMIZE", "PRODUCT_TELEMETRY": "ANONYMIZE", "USER_EXPORT": "DELETE",
             "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT", "SECURITY_AUDIT_LOG": "RETAIN",
             "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "BLOCK", "ACCOUNT_TOMBSTONE": "REDACT",
             "DATA_FLOW": "DISABLE"}


def category(system, observations):
    attrs = {}
    kinds = set()
    for o in observations:
        attrs.update({k: v for k, v in o["attrs"].items() if v is not None})
        kinds.add(o["kind"])
    rt = attrs.get("record_type")
    if rt in TREATMENT and rt not in ("SUPPORT_MESSAGE", "ANALYTICS_PROFILE"):
        return rt
    if system == "account_service":
        return "USER_PROFILE"
    if system == "search_service":
        return "SEARCH_INDEX_ENTRY"
    if system == "cache_service":
        return "CACHE_ENTRY"
    if system == "export_service":
        return "USER_EXPORT"
    if system == "marketing_platform":
        return "MARKETING_PROFILE"
    if system == "email_delivery_service":
        return "EMAIL_DELIVERY_LOG"
    if system == "security_service":
        return "SECURITY_AUDIT_LOG"
    if system == "legal_service":
        return "LEGAL_HOLD_RECORD"
    if system == "backup_service":
        return "BACKUP_ARCHIVE"
    if system == "integration_service":
        return "DATA_FLOW"
    if system == "identity_service":
        return "ACCOUNT_TOMBSTONE"
    if system in ("support_platform", "object_storage"):
        if rt == "SUPPORT_ATTACHMENT" or attrs.get("attachment") or "ticket_attachments" in kinds \
                or system == "object_storage":
            return "SUPPORT_ATTACHMENT"
        if attrs.get("secondary_index"):
            return "SECONDARY_IDENTIFIER"
        return "SUPPORT_MESSAGE"
    if system == "analytics_platform":
        if attrs.get("contact_record"):
            return "SECONDARY_IDENTIFIER"
        return "ANALYTICS_PROFILE"
    return "USER_PROFILE"


def visible(o, audit):
    """Evidence usable for a case: captured and observed no later than audit_at."""
    return ts(o["cap"]) <= audit and ts(o["t"]) <= audit


def latest(observations):
    return max(observations, key=lambda o: (o["t"], o["cap"], o["file"])) if observations else None


def exposed(o, cat):
    if o["exposed"] is None:
        e = bool(o["direct"])
    else:
        e = o["exposed"]
    if cat in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY") and o["refs"]:
        return True  # still attributable through the internal user reference
    return e


def assess(rid, system, obs_all, scans, audit):
    """Return dict(category, finding, remediation, verification, evidence) for one resource."""
    vis = [o for o in obs_all if visible(o, audit)]
    cat = category(system, obs_all)
    treat = TREATMENT[cat]
    last = latest(vis)
    res = dict(rid=rid, system=system, category=cat, finding=None, remediation=None, verification=None,
               evidence="", last=last)

    def absent_scan():
        cands = [s for s in scans if s[2] == system and ts(s[1]) <= audit and ts(s[4]) <= audit and rid not in s[5]]
        return max(cands, key=lambda s: (s[4], s[1]))[0] if cands else ""

    if treat == "DELETE":
        res["verification"] = "VERIFY_RECORD_ABSENT"
        if last and last["present"]:
            res.update(finding="REPORT_REMAINING_RECORD", remediation="DELETE_RECORD", evidence=last["file"])
        elif last:
            res["evidence"] = last["file"]
        else:
            res["evidence"] = absent_scan()
    elif treat in ("REDACT", "ANONYMIZE"):
        res["verification"] = "VERIFY_RECORD_REDACTED" if treat == "REDACT" else "VERIFY_RECORD_ANONYMIZED"
        if last and last["present"] and exposed(last, cat):
            res.update(finding="REPORT_EXPOSED_PII", remediation=treat + "_RECORD", evidence=last["file"])
        elif last:
            res["evidence"] = last["file"]
    elif treat == "RETAIN":
        res["verification"] = "VERIFY_RECORD_PRESENT"
        ru = None
        for o in obs_all:
            ru = o["attrs"].get("retention_until") or ru
        if ru and ts(ru) < audit:
            res["verification"] = "VERIFY_RECORD_ABSENT"
            if last and last["present"]:
                res.update(finding="REPORT_REMAINING_RECORD", remediation="DELETE_RECORD", evidence=last["file"])
        if last:
            res["evidence"] = res["evidence"] or last["file"]
    elif treat == "DISABLE":
        res["verification"] = "VERIFY_DATA_FLOW_DISABLED"
        flows = [o for o in vis if o["attrs"].get("enabled") is not None]
        lf = latest(flows)
        if lf and lf["attrs"]["enabled"]:
            res.update(finding="REPORT_ACTIVE_DATA_FLOW", remediation="DISABLE_DATA_FLOW", evidence=lf["file"])
        elif lf:
            res["evidence"] = lf["file"]
    elif treat == "BLOCK":
        res["verification"] = "VERIFY_RESTORE_BLOCKED"
        bs = [o for o in vis if o["attrs"].get("restore_allowed") is not None]
        lb = latest(bs)
        if lb and lb["attrs"]["restore_allowed"]:
            res.update(finding="REPORT_RESTORE_RISK", remediation="BLOCK_RESTORE", evidence=lb["file"])
        elif lb:
            res["evidence"] = lb["file"]
    return res


def build_steps(assessments):
    """Turn per-resource assessments into (status, ordered step list)."""
    finds = [a for a in assessments if a["finding"]]
    status = "INCOMPLETE" if finds else "COMPLETE"
    steps = []

    def add(phase, op, a, ev):
        steps.append(dict(step_no=len(steps) + 1, phase=phase, system_id=a["system"], resource_id=a["rid"],
                          operation=op, state=STATE[op], evidence_id=ev))

    def key(order, field):
        return lambda a: (order.index(a[field]), a["system"], a["rid"])

    if status == "INCOMPLETE":
        for a in sorted(finds, key=key(FIND_ORDER, "finding")):
            add("FINDING", a["finding"], a, a["evidence"])
        for a in sorted(finds, key=key(REM_ORDER, "remediation")):
            add("REMEDIATION", a["remediation"], a, "")
        for a in sorted([a for a in assessments if a["verification"]], key=key(VER_ORDER, "verification")):
            add("VERIFICATION", a["verification"], a, "")
    else:
        for a in sorted([a for a in assessments if a["verification"]], key=key(VER_ORDER, "verification")):
            add("VERIFICATION", a["verification"], a, a["evidence"])
    return status, steps
