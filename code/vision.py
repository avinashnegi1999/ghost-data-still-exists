"""Read screenshots and PDF business documents into status rows with a vision model.

Each media file is sent once to Gemini with a strict response schema. The answer is validated
(allowed systems / record types / statuses only) and cached. Rows feed evidence.status_obs exactly
like the plain-text handover rows, so downstream reasoning is identical for every evidence format.
If the model is unavailable and nothing is cached, the file contributes no observations: the
auditor then cannot claim anything from it, which keeps the decision conservative.
"""
import os
import re

import evidence
import llm

SYSTEMS = ["account_service", "search_service", "support_platform", "analytics_platform", "export_service",
           "integration_service", "backup_service", "security_service", "legal_service", "identity_service",
           "marketing_platform", "object_storage", "email_delivery_service", "cache_service"]
RECORD_TYPES = ["USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY", "SUPPORT_MESSAGE",
                "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "USER_EXPORT", "MARKETING_PROFILE",
                "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE",
                "ACCOUNT_TOMBSTONE", "DATA_FLOW"]
STATUSES = ["FOUND", "NOT_FOUND", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED", "BLOCKED"]

PROMPT = """You are extracting facts from an internal business artifact of the fictional retailer Northstar Shop
for a personal-data deletion audit. Do not judge whether deletion is complete; only report what the artifact shows.

List every customer-data record the artifact displays (table rows and the detail panel). For each record give:
- system: the platform that holds it, one of: {systems}
- resource_id: the internal id exactly as printed (e.g. account_5762, analytics_7438, data_flow_1121, backup_9287,
  search_428, support_9538, export_1260, audit_record_003, tombstone_17, marketing_001). If only a file name,
  order number or statement number is printed, leave resource_id empty and put that text in display_reference.
- display_reference: file name, order/statement/ticket number or other printed label for the record ("" if none).
- record_type: one of {types}
- status: one of {statuses}. Use FOUND when the record exists; IDENTIFIABLE when it exists and shows a person's
  name, email, phone or hash; ANONYMIZED / REDACTED when identifiers are visibly removed; ACTIVE for a running
  sync/connector or for a backup whose restore is possible/not blocked; DISABLED for a stopped connector; BLOCKED
  for a backup whose restore is blocked or denied; NOT_FOUND when the artifact says the record is gone.
- subject: the customer email or internal user reference (user_123) the record belongs to ("" if none).
- identifiers: every email, phone number, user_123 reference and person name printed for that record.
- retain_until: ISO date if a retention/retain-until date is printed, else "".
Return only records about individual customers; skip aggregate metrics and generic platform connectors with no
customer selector."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "captured_at": {"type": "STRING"},
        "records": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "system": {"type": "STRING", "enum": SYSTEMS},
            "resource_id": {"type": "STRING"},
            "display_reference": {"type": "STRING"},
            "record_type": {"type": "STRING", "enum": RECORD_TYPES},
            "status": {"type": "STRING", "enum": STATUSES},
            "subject": {"type": "STRING"},
            "identifiers": {"type": "ARRAY", "items": {"type": "STRING"}},
            "retain_until": {"type": "STRING"}},
            "required": ["system", "resource_id", "display_reference", "record_type", "status", "subject",
                         "identifiers", "retain_until"]}}},
    "required": ["records"],
}


def _validate(obj):
    recs = obj.get("records")
    if not isinstance(recs, list):
        raise ValueError("records missing")
    for r in recs:
        if r.get("system") not in SYSTEMS or r.get("record_type") not in RECORD_TYPES \
                or r.get("status") not in STATUSES:
            raise ValueError("value outside the allowed vocabulary: %r" % r)
        rid = r.get("resource_id") or ""
        if rid and (not re.fullmatch(r"[a-z_]+_\w+", rid) or rid.startswith("user_")):
            # user_123 is a subject reference, never a record id (tombstones are resolved via the index)
            # a printed label (order / statement number) in the id slot: keep it as a display reference
            r["display_reference"] = r.get("display_reference") or rid
            r["resource_id"] = ""


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "pdf": "application/pdf"}[ext[1:]]
    prompt = PROMPT.format(systems=", ".join(SYSTEMS), types=", ".join(RECORD_TYPES), statuses=", ".join(STATUSES))
    return llm.generate_json("media_extraction", prompt, SCHEMA, _validate,
                             attachment=open(path, "rb").read(), mime=mime)


def display_index(activity):
    """Printed label -> resource id, from catalog rows like 'linked export_1842 (NST-2026-0718-1842)'."""
    out = {}
    for a in activity:
        if a["event_type"] == "LINK_RESOURCE_IDENTITY":
            m = re.search(re.escape(a["resource_id"]) + r" \(([^)]+)\)", a["details"])
            if m:
                out[m.group(1).strip().lower()] = a["resource_id"]
    return out


def media_rows(dataset_dir, manifest, display_index):
    """{file_id: [status rows]} for every image/PDF. display_index maps printed labels -> resource ids."""
    out = {}
    for m in manifest:
        path = os.path.join(dataset_dir, m["file_path"])
        if os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg", ".pdf") or not os.path.exists(path):
            continue
        try:
            res = extract(path)
        except RuntimeError as e:
            print("WARN media extraction unavailable for", m["file_id"], "-", e)
            continue
        rows = []
        for r in res["records"]:
            rid = r["resource_id"]
            label = (r.get("display_reference") or "").strip().lower()
            if not rid or rid.startswith("user_") or not re.fullmatch(r"[a-z_]+_\w+", rid):
                # cached answers predate the lenient validator: normalise here too
                rid = display_index.get(label) or display_index.get(rid.lower(), "")
            if not rid:
                # a file name printed alongside the id, e.g. "delivery_confirmation_19.pdf"
                rid = next((v for k, v in display_index.items() if k and k in label), "")
            if not rid:
                continue
            # the resource id prefix fixes system and category; the model's guess is kept only where the
            # prefix is ambiguous (support messages vs attachments)
            system = evidence.system_of(rid) or r["system"]
            rtype = r["record_type"] if system == "support_platform" else ""
            if system == "support_platform" and path.lower().endswith(".pdf"):
                rtype = "SUPPORT_ATTACHMENT"  # the document itself is the uploaded file
            rows.append(dict(system=system, resource_id=rid, record_type=rtype, status=r["status"],
                             subject=r["subject"], identifiers=r["identifiers"], observed_at=m["captured_at"],
                             retain_until=r["retain_until"]))
        out[m["file_id"]] = rows
    return out
