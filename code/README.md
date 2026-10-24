# Ghost Data Still Exists — deletion-audit agent

For every request in `dataset/deletion_requests.csv` the agent decides whether deletion was asked for, finds every
copy of the person's data that evidence available at `audit_at` connects to them, applies `deletion_rules.csv`, and
writes `COMPLETE` / `INCOMPLETE` / `NOT_APPLICABLE` with an ordered FINDING → REMEDIATION → VERIFICATION plan.

## Run

Python 3.10+; standard library only (no `pip install`). `pdftotext` is not required.

```bash
python code/main.py               # writes ./output.csv (518 rows) and code/evaluation/usage_report.md
python code/evaluation/main.py    # scores the 32 public samples + custom cases, writes evaluation/results.json
```

Model answers are cached in `code/cache/llm_cache.json`, so both commands run offline and deterministically.
To re-run the model calls, delete the cache and set the key in the environment (never in code):

```bash
export GEMINI_API_KEY=...         # Google AI Studio key; optional GEMINI_MODEL (default gemini-2.5-flash)
```

Without a key and without cache, the agent still runs: intent uses the keyword guard alone and screenshots/PDFs
contribute no observations (so nothing is claimed from them).

## Architecture

| Module | Role |
|---|---|
| `data.py` | CSV loading; `Identities` rebuilds every current + historical email, phone, email hash and user ref per user. |
| `logparse.py` | Parses the six log dialects (JSON lines, bracketed, pipe, syslog, cloud-runtime, key=value). |
| `evidence.py` | Normalises JSON, SQL (run in in-memory SQLite, replica vs primary), logs, text handovers and media rows into one observation shape: resource, system, time, presence, exposed identifiers, retention, scans (full listings that prove absence). |
| `vision.py` | **Model step.** Screenshots and PDFs go to Gemini with a strict enum schema; answers are validated, printed labels (order / statement numbers, file names) are mapped to resource ids through the data catalog. |
| `intent.py` | **Model step.** Batched multilingual classification of request texts (strict schema). A keyword guard runs on every text; deletion is audited only when both agree. |
| `linker.py` | Connects resources to the subject: identifiers in evidence, catalog links, and deletion-worker actions assigned by global elimination across concurrent cases. |
| `engine.py` | Per-resource state at `audit_at` (latest visible observation; later evidence ignored), policy category → required treatment, and the step plan. |
| `pipeline.py` | Ties it together per case. |
| `llm.py` | REST client: strict JSON, validation + retries, per-model fallback on quota errors, disk cache, token accounting. |

### Decision rules (build contract)

* No authorized deletion or unknown requester → `NOT_APPLICABLE`, `[]`.
* A resource is relevant if participant-visible evidence (captured ≤ `audit_at`) or a catalog link ties it to any
  of the subject's identifiers, within a time window around the request.
* State per resource = latest observation visible at `audit_at`; absence is proven only by a full listing that does
  not contain it. Workflow results are claims and never prove state.
* Treatment comes from `deletion_rules.csv` by category: delete / redact / anonymize / retain (expired retention →
  delete) / tombstone (keep subject ref, strip direct identifiers) / disable flow / block restore.
* Any violation → `INCOMPLETE`: findings (cite evidence) → remediation (flows and restores first, then records,
  downstream before source) → verification for every relevant resource. Otherwise `COMPLETE` with evidence-cited
  verifications.
* Conservative fallbacks: a missing or invalid model answer contributes nothing (no invented findings); intent
  disagreement resolves to not running a deletion.
