"""Normalize every evidence file into a flat list of observations.

An observation is one statement an evidence file makes about one resource:

    {file, t, system, rid, kind, present, exposed, direct, refs, lookup, attrs}

* present  True / False / None  - does the record exist at time t?
* exposed  True / False / None  - does the record still carry direct identifiers?
* direct   identifiers (email / phone / hash / name) seen inside the record
* refs     stable internal user references (user_123) seen inside the record
* lookup   identifiers that were used to find the record (lookup / correlation keys)
* attrs    enabled, restore_allowed, retention_until, record_type, status, ...

A "scan" is a full listing of one system's collection at time t; resources of that
system missing from it are evidenced absent (used for COMPLETE verification citations).
"""
import csv
import json
import os
import re
import sqlite3
from collections import defaultdict

from logparse import parse_line

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USERREF = re.compile(r"^user_\d+$")
HASH = re.compile(r"^[0-9a-f]{64}$")
PHONE = re.compile(r"^\+?[\d][\d\s().-]{6,}$")
RESOURCE = re.compile(r"^(account|search|support|analytics|export|data_flow|backup|audit_record|legal_hold|"
                      r"tombstone|marketing|email_delivery|cache|telemetry|object)_\w+$")

SYSTEM_BY_PREFIX = {
    "account": "account_service", "search": "search_service", "support": "support_platform",
    "analytics": "analytics_platform", "export": "export_service", "data_flow": "integration_service",
    "backup": "backup_service", "audit_record": "security_service", "legal_hold": "legal_service",
    "tombstone": "identity_service", "marketing": "marketing_platform",
    "email_delivery": "email_delivery_service", "cache": "cache_service", "telemetry": "telemetry_service",
    "object": "object_storage",
}
NULLS = {None, "", "null", "none", "NULL", "None", "[REDACTED]", "no direct identifier"}
# keys whose value is the stable internal reference that policy allows to remain
REF_KEYS = {"customer_reference", "subject_reference", "user_id", "subject_selector", "subject_ref"}
LOOKUP_KEYS = {"lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref",
               "correlation_key", "query", "lookup_value"}
# keys that never carry a personal identifier
SKIP_KEYS = {"resource_id", "observed_at", "recorded_at", "captured_at", "updated_at", "created_at", "expires_at",
             "retention_until", "retained_until", "event_code", "event_name", "actor_type", "integrity_seal",
             "index_name", "index", "audience_cohort", "connector_name", "destination", "mode", "archive_key",
             "archive_path", "object_key", "encryption", "storage_class", "ticket_id", "channel",
             "last_delivery_status", "account_status", "signup_channel", "record_type", "result", "system",
             "reporting_window", "metrics", "recent_activity", "included_sections", "file_name", "order_reference",
             "retention_reason", "status_code", "enabled", "restore_allowed", "download_available", "source_resource_id",
             "indexed_at", "message_excerpt", "origin_system", "collection_name"}


def system_of(rid):
    for p in sorted(SYSTEM_BY_PREFIX, key=len, reverse=True):
        if rid.startswith(p + "_"):
            return SYSTEM_BY_PREFIX[p]
    return None


def norm_phone(v):
    return "+" + re.sub(r"\D", "", v)


def classify(value):
    """Return (kind, normalized) for one identifier-ish string, or None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v in NULLS:
        return None
    if USERREF.match(v):
        return ("ref", v)
    if EMAIL.fullmatch(v):
        return ("email", v.lower())
    if HASH.match(v.lower()):
        return ("hash", v.lower())
    if PHONE.match(v) and sum(c.isdigit() for c in v) >= 7:
        return ("phone", norm_phone(v))
    return None


def free_text_ids(text):
    out = set()
    if not isinstance(text, str):
        return out
    for e in EMAIL.findall(text):
        out.add(e.lower())
    for p in re.findall(r"\+\d[\d\s().-]{7,}\d", text):
        out.add(norm_phone(p))
    return out


def truthy(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "allowed", "allow", "enabled", "active"):
        return True
    if s in ("0", "false", "no", "denied", "deny", "disabled", "blocked", "stopped"):
        return False
    return None


def new_obs(file, t, system, rid, kind):
    return dict(file=file, t=t, system=system, rid=rid, kind=kind, present=True, exposed=None,
                direct=set(), refs=set(), lookup=set(), names=set(), attrs={})


def absorb_record(o, rec, name_keys=("full_name", "display_name", "requester_name", "name")):
    """Pull identifiers and state attributes out of a (possibly nested) record dict."""
    def walk(k, v):
        if isinstance(v, dict):
            for kk, vv in v.items():
                walk(kk, vv)
            return
        if isinstance(v, list):
            for x in v:
                walk(k, x)
            return
        if k in SKIP_KEYS:
            return
        if k in name_keys:
            if isinstance(v, str) and v.strip() not in NULLS:
                o["names"].add(v.strip().lower())
                o["direct"].add("name:" + v.strip().lower())
            return
        if k in ("message_text", "message", "matched_value") and isinstance(v, str):
            o["direct"].update(free_text_ids(v))
            c = classify(v)
            if c and c[0] != "ref":
                o["direct"].add(c[1])
            return
        c = classify(v) if isinstance(v, str) else None
        if not c:
            return
        if c[0] == "ref" or k in REF_KEYS and c[0] == "ref":
            o["refs"].add(c[1])
        elif k in LOOKUP_KEYS:
            o["lookup"].add(c[1])
        else:
            o["direct"].add(c[1])

    walk("", rec)
    for k in ("retention_until", "retained_until"):
        if rec.get(k):
            o["attrs"]["retention_until"] = rec[k]
    for k in ("enabled",):
        if k in rec and truthy(rec[k]) is not None:
            o["attrs"]["enabled"] = truthy(rec[k])
    if "restore_allowed" in rec and truthy(rec["restore_allowed"]) is not None:
        o["attrs"]["restore_allowed"] = truthy(rec["restore_allowed"])
    for k in ("connector_name", "event_code", "event_name", "account_status", "file_name", "storage_class",
              "message_excerpt", "mode", "destination", "order_reference", "retention_reason"):
        if rec.get(k) not in NULLS:
            o["attrs"][k] = rec[k]
    if rec.get("account_status") in ("DELETED",):
        o["attrs"]["tombstone"] = True
    if rec.get("download_available") is False:
        o["present"] = False
    if o["direct"]:
        o["exposed"] = True
    elif o["exposed"] is None:
        o["exposed"] = False


STATUS_RESULT = {
    "FOUND": dict(present=True), "NOT_FOUND": dict(present=False),
    "IDENTIFIABLE": dict(present=True, exposed=True), "ANONYMIZED": dict(present=True, exposed=False),
    "REDACTED": dict(present=True, exposed=False), "ACTIVE": dict(present=True), "DISABLED": dict(present=True),
    "BLOCKED": dict(present=True), "PRESENT": dict(present=True), "ABSENT": dict(present=False),
    "DELETED": dict(present=False), "RETAINED": dict(present=True),
}


def status_obs(file, t, system, rid, record_type, result, subject):
    """Rows of the form system | resource | RECORD_TYPE | RESULT | subject."""
    system = system or system_of(rid)
    o = new_obs(file, t, system, rid, "status:" + (record_type or ""))
    result = (result or "").strip().upper()
    for k, v in STATUS_RESULT.get(result, {}).items():
        o[k] = v
    o["attrs"]["record_type"] = record_type
    o["attrs"]["status"] = result
    if record_type == "DATA_FLOW" or system == "integration_service":
        if result in ("ACTIVE", "DISABLED"):
            o["attrs"]["enabled"] = result == "ACTIVE"
    if record_type == "BACKUP_ARCHIVE" or system == "backup_service":
        if result in ("ACTIVE", "BLOCKED"):
            o["attrs"]["restore_allowed"] = result == "ACTIVE"
    c = classify(subject or "")
    if c:
        # the subject column names who the record concerns; for IDENTIFIABLE/FOUND rows it is a
        # direct identifier still held, otherwise just the key the operator looked the record up with
        if c[0] == "ref":
            o["refs"].add(c[1])
        else:
            o["lookup"].add(c[1])
            if result in ("IDENTIFIABLE",):
                o["direct"].add(c[1])
    if o["exposed"] is None and result in ("FOUND", "ACTIVE", "DISABLED", "BLOCKED"):
        o["exposed"] = bool(o["direct"]) if record_type not in ("USER_PROFILE", "SEARCH_INDEX_ENTRY") else None
    return o


# ---------------------------------------------------------------- JSON

def parse_json(file, path, captured_at):
    d = json.load(open(path, encoding="utf-8"))
    obs, scans = [], []
    file_t = d.get("exported_at") or d.get("captured_at") or captured_at

    if "service_exports" in d:
        for system, colls in d["service_exports"].items():
            for coll, recs in colls.items():
                scans.append((system, coll, file_t, {r["resource_id"] for r in recs}))
                for r in recs:
                    o = new_obs(file, r.get("observed_at") or file_t, system, r["resource_id"], coll)
                    if coll == "profile_lookups":
                        if r.get("status_code") == 404 or r.get("profile") is None:
                            o["present"] = False
                        absorb_record(o, r.get("profile") or {})
                    elif coll == "index_queries":
                        hits = (r.get("hits") or {})
                        docs = [x for x in hits.get("documents", []) if x.get("resource_id") == r["resource_id"]]
                        o["present"] = bool(docs) or bool(hits.get("total"))
                        c = classify(r.get("query") or "")
                        if c:
                            o["lookup"].add(c[1])
                        for doc in docs:
                            absorb_record(o, doc)
                    else:
                        absorb_record(o, {k: v for k, v in r.items() if k != "observed_at"})
                    obs.append(o)
        return obs, scans

    if "snapshots" in d:
        for s in d["snapshots"]:
            st = s.get("captured_at") or file_t
            system = s.get("data_store")
            recs = s.get("records", [])
            if recs and "record_type" in recs[0]:
                for r in recs:
                    obs.append(status_obs(file, r.get("recorded_at") or st, r.get("system"), r["resource_id"],
                                          r.get("record_type"), r.get("result"), r.get("lookup_value")))
                continue
            scans.append((system, s.get("collection"), st, {r["resource_id"] for r in recs}))
            for r in recs:
                o = new_obs(file, st, system or system_of(r["resource_id"]), r["resource_id"], s.get("collection"))
                absorb_record(o, r)
                if s.get("collection") == "ticket_attachments":
                    o["attrs"]["attachment"] = True
                obs.append(o)
        return obs, scans

    recs = d.get("records", [])
    if recs and "record_type" in recs[0]:
        for r in recs:
            obs.append(status_obs(file, r.get("recorded_at") or file_t, r.get("system"), r["resource_id"],
                                  r.get("record_type"), r.get("result"), r.get("subject_reference")))
        return obs, scans
    coll = d.get("collection")
    for r in recs:
        system = system_of(r["resource_id"])
        o = new_obs(file, file_t, system, r["resource_id"], coll)
        absorb_record(o, r)
        obs.append(o)
    if recs:
        scans.append((system_of(recs[0]["resource_id"]), coll, file_t, {r["resource_id"] for r in recs}))
    return obs, scans


# ---------------------------------------------------------------- SQL

TABLE_SYSTEM = [("analytics", "analytics_platform"), ("profile", "account_service"), ("search", "search_service"), ("analytics", "analytics_platform"),
                ("support", "support_platform"), ("export", "export_service"), ("connector", "integration_service"),
                ("backup", "backup_service"), ("security", "security_service"), ("legal", "legal_service"),
                ("tombstone", "identity_service"), ("marketing", "marketing_platform")]


def parse_sql(file, path, captured_at):
    text = open(path, encoding="utf-8").read()
    m = re.search(r"Captured at: (\S+)", text)
    file_t = m.group(1) if m else captured_at
    db = sqlite3.connect(":memory:")
    db.executescript(text)
    scope = {}
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
    if "snapshot_scope" in tables:
        scope = dict(db.execute("select origin_system, captured_at from snapshot_scope"))
    obs, scans = [], []
    for t in tables:
        if t == "snapshot_scope":
            continue
        cur = db.execute(f'select * from "{t}"')
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur]
        topo = "replica" if "replica" in t else "primary"
        by_sys = defaultdict(set)
        tsys = next((s for k, s in TABLE_SYSTEM if k in t), None)
        for r in rows:
            rec = json.loads(r["record_json"]) if "record_json" in r else dict(r)
            system = r.get("origin_system") or system_of(r["resource_id"]) or tsys
            st = scope.get(system, file_t)
            o = new_obs(file, st, system, r["resource_id"], r.get("collection_name") or t)
            o["attrs"]["table"] = t
            o["attrs"]["topology"] = topo
            rec.pop("updated_at", None)
            absorb_record(o, rec)
            if system == "integration_service" and "enabled" in rec:
                o["attrs"]["enabled"] = truthy(rec["enabled"])
            if system == "backup_service" and "restore_allowed" in rec:
                o["attrs"]["restore_allowed"] = truthy(rec["restore_allowed"])
            if r.get("source_resource_id"):
                o["attrs"]["source"] = r["source_resource_id"]
            by_sys[system].add(r["resource_id"])
            obs.append(o)
        systems = set(by_sys) | ({tsys} if tsys else set())
        if "record_json" in cols and not rows:
            systems = set()
        for s in systems:
            scans.append((s, t, scope.get(s, file_t), by_sys.get(s, set())))
    # inventory snapshots declare their scope even for systems with no table rows
    for s, st in scope.items():
        if not any(sc[0] == s for sc in scans):
            scans.append((s, "scope", st, set()))
    return obs, scans


# ---------------------------------------------------------------- logs

def parse_log(file, path, captured_at):
    traces = defaultdict(list)
    singles = []
    for line in open(path, encoding="utf-8"):
        p = parse_line(line)
        if not p or not p.get("event"):
            continue
        if p["trace"] and p["trace"].startswith(("tr-", "rb-")):
            traces[p["trace"]].append(p)
        else:
            singles.append(p)
    obs = []
    for p in singles:
        kv = p["kv"]
        if p["event"] == "record_lookup" and kv.get("resource_id"):
            obs.append(status_obs(file, p["ts"], kv.get("system"), kv["resource_id"], kv.get("record_type"),
                                  kv.get("result"), kv.get("subject_reference")))
        elif kv.get("resource_id"):
            obs.extend(_log_event(file, p, set()))
    for trace, lines in traces.items():
        keys = set()
        for p in lines:
            for k in ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref"):
                c = classify(p["kv"].get(k, ""))
                if c:
                    keys.add(c[1])
        for p in lines:
            if p["kv"].get("resource_id"):
                obs.extend(_log_event(file, p, keys))
    return obs, []


def _log_event(file, p, keys):
    kv, ev, svc = p["kv"], p["event"], p["service"]
    rid = kv["resource_id"]
    system = system_of(rid) or svc
    if svc == "profile_service":
        return []  # bulk preference-service read traffic, not the account store itself
    if ev.endswith("_started") or ev in ("cache_lookup", "connection_acquired"):
        return []
    o = new_obs(file, p["ts"], system, rid, ev)
    o["lookup"].update(keys)
    c = classify(kv.get("correlation_key", ""))
    if c:
        (o["refs"] if c[0] == "ref" else o["lookup"]).add(c[1])
    c = classify(kv.get("lookup_key", ""))
    if c:
        (o["refs"] if c[0] == "ref" and ev.endswith("completed") and False else o["lookup"]).add(c[1])
    rec = {k: v for k, v in kv.items() if k not in ("instance", "pod", "region", "attempt", "took_ms", "duration_ms",
                                                    "shard", "wait_ms", "latency_ms", "service", "event", "severity",
                                                    "logger", "trace_id", "level", "resource_id", "correlation_key",
                                                    "lookup_key", "detector", "integrity_check", "rows",
                                                    "http_status", "result", "total_hits", "available", "file",
                                                    "eligible", "deletion_block", "scheduler_registered", "last_run",
                                                    "decision", "policy", "direct_identifiers", "email_opt_in",
                                                    "archive_present", "marker_found", "last_write", "reason",
                                                    "access", "retry", "source", "seq")}
    absorb_record(o, rec)
    if "rows" in kv:
        o["present"] = kv["rows"] not in ("0",)
    if "http_status" in kv:
        o["present"] = kv["http_status"].startswith("2")
    if "result" in kv:
        o["present"] = kv["result"].lower() in ("hit", "found")
    if "available" in kv:
        o["present"] = truthy(kv["available"]) is not False
    if "archive_present" in kv:
        o["present"] = truthy(kv["archive_present"]) is not False
    if ev in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"):
        o["exposed"] = True
        o["direct"].update(free_text_ids(kv.get("matched_value", "")))
        c = classify(kv.get("matched_value", ""))
        if c:
            o["direct"].add(c[1])
    if ev == "attachment_pii_scan" or kv.get("file"):
        o["attrs"]["attachment"] = True
        o["attrs"]["file_name"] = kv.get("file")
    if "direct_identifiers" in kv:
        o["exposed"] = kv["direct_identifiers"] not in ("0",)
    if ev == "restore_candidate_evaluated":
        o["attrs"]["restore_allowed"] = truthy(kv.get("eligible")) and not truthy(kv.get("deletion_block"))
    if ev == "restore_authorization_completed":
        o["attrs"]["restore_allowed"] = kv.get("decision") == "allow"
    if "enabled" in kv:
        o["attrs"]["enabled"] = truthy(kv["enabled"])
    if ev == "contact_lookup_completed":
        o["attrs"]["contact_record"] = True
    if ev == "deletion_marker_check":
        o["present"] = not truthy(kv.get("marker_found"))
    if ev in ("contact_query_completed",):
        o["attrs"]["secondary_index"] = kv.get("source") or True
    if ev == "retained_order_loaded":
        o["attrs"]["legal_hold"] = kv.get("access")
    return [o]


# ---------------------------------------------------------------- plain text handover

HANDOVER = re.compile(r"^- (\S+) \| (\S+) \| (\S+) \| (\S+) \| (\S+) \| (.+)$")


def parse_text(file, path, captured_at):
    obs = []
    for line in open(path, encoding="utf-8"):
        m = HANDOVER.match(line.strip())
        if m:
            t, system, rid, rtype, result, subject = m.groups()
            obs.append(status_obs(file, t, system, rid, rtype, result, subject))
    return obs, []


# ---------------------------------------------------------------- loader

def load_all(dataset_dir, media_rows=None):
    """Parse every manifest file. media_rows: {file_id: [status rows]} from the vision extractor."""
    manifest = list(csv.DictReader(open(os.path.join(dataset_dir, "evidence_files.csv"), encoding="utf-8")))
    obs, scans, files = [], [], {}
    for m in manifest:
        fid, path, cap = m["file_id"], os.path.join(dataset_dir, m["file_path"]), m["captured_at"]
        files[fid] = m
        ext = os.path.splitext(path)[1].lower()
        if not os.path.exists(path):
            continue
        if ext == ".json":
            o, s = parse_json(fid, path, cap)
        elif ext == ".sql":
            o, s = parse_sql(fid, path, cap)
        elif ext == ".log":
            o, s = parse_log(fid, path, cap)
        elif ext == ".txt":
            o, s = parse_text(fid, path, cap)
        else:
            o, s = [], []
            for r in (media_rows or {}).get(fid, []):
                if r.get("resource_id"):
                    ob = status_obs(fid, r.get("observed_at") or cap, r.get("system"), r["resource_id"],
                                    r.get("record_type"), r.get("status"), r.get("subject"))
                    ob["attrs"]["media"] = True
                    for extra in r.get("identifiers", []) or []:
                        c = classify(extra)
                        if c:
                            (ob["refs"] if c[0] == "ref" else ob["lookup"]).add(c[1])
                    o.append(ob)
        for ob in o:
            ob["cap"] = cap
        obs.extend(o)
        scans.extend((fid, cap) + tuple(sc) for sc in s)
    return obs, scans, files
