# Evaluation report

Reproduce: `python code/evaluation/main.py` (offline from the model cache; writes `evaluation/results.json`).
Scorer: `evaluation/score.py`. Gold = the 32 public answers in `dataset/sample_output.csv`. The full 518-case
pool is loaded as context (workflow-action assignment is global), but only samples are scored. No organizer
files are read, and no sample answer is used by the agent at prediction time.

## Metrics on the 32 public samples (final system)

| Metric | Value |
|---|---|
| Case-level deletion-status accuracy | **100.0 %** (32/32) |
| Exact case match (status + every step, in order) | **87.5 %** (28/32) |
| Step precision / recall / F1 (multiset of full steps) | 0.995 / 0.979 / 0.987 |
| Sequence accuracy (mean SequenceMatcher ratio of ordered steps) | 0.982 |
| Field accuracy, position-aligned — phase | 0.984 |
| system_id | 0.959 |
| resource_id | 0.953 |
| operation | 0.959 |
| state | 0.959 |
| evidence_id | 0.979 |
| Cited evidence captured after `audit_at` | 0 violations |

### Remaining misses (all four keep the correct status)

| Case | Miss | Why |
|---|---|---|
| case_04 | verification of `account_004` | The resource id appears in no participant-visible file or activity row the agent can tie to the subject. |
| case_18, case_20 | one retained `audit_record_*` | Same: the record id is not reachable from the subject's identifiers. |
| case_23 | `account_294` instead of `account_410` | Two concurrent cases, two deletion-worker account actions, no evidence separating them: the tie is paired by numeric order (marked `ponytail:` in `linker.resolve_ties`). |

We did not hard-code any of these.

## Participant-built evaluation cases

Each case comes from a public sample through one controlled change whose correct effect is known in advance.
The pool keeps one subject per case, so the change is tested in isolation.

| # | Case | Change | Expected | Result |
|---|---|---|---|---|
| 1 | custom_1 | case_01 text replaced by a phone-number correction that says "do not delete anything" | `NOT_APPLICABLE`, `[]` | pass |
| 2 | custom_2 | case_02 text rewritten in Spanish ("Solicito la eliminación definitiva…") | identical status + steps to case_02 | pass |
| 3 | custom_3 | case_10 sent from the subject's **historical** email (from `user_audit_log.csv`) | identical status + steps to case_10 | pass |
| 4 | custom_4 | case_03 sent from an email no identity has ever held | `NOT_APPLICABLE` | pass |
| 5 | all samples | Every cited `evidence_id` is captured at or before that case's `audit_at` | 0 violations | pass |

Full-run structural checks on all 518 rows (from `output.csv`):
- every row is present, sorted, and has valid JSON;
- steps are numbered from 1 with phases in order;
- `NOT_APPLICABLE` appears if and only if `[]`;
- no `INCOMPLETE` case lacks findings;
- no `COMPLETE` step has an empty evidence id.

Status mix: 354 INCOMPLETE, 154 COMPLETE, 10 NOT_APPLICABLE.

## Failures found and improvements made

| Step | Exact match | Failure found | Fix |
|---|---|---|---|
| First end-to-end rules engine | 62.5 % | Pipe-delimited logs were split into the wrong number of fields. SQL analytics tables were attributed to account_service. | Fixed the log parser (split into 5 fields). System precedence is now origin column, then id prefix, then table. |
| Identity linking | 78 % | Records were found via a `subject_ref` lookup key but never linked. Catalog links attached old objects to later, unrelated requests. | Linking now uses refs, direct identifiers and lookup keys. Catalog links count only within ±45 days of the request window. |
| Workflow-action assignment | 84.4 % | Deletion-worker rows do not name the subject. Naive time windows gave the same account to several cases. | Global elimination across all concurrent cases. Candidates must co-occur in the case's evidence file. A record with no identifier is given only to a subject whose audit is otherwise clean. |
| Vision step added (screenshots/PDFs) | 84.4 % → 87.5 % | The model put display labels (`NST-2026-0718-1842`, `user_18`) in the resource-id slot. It typed a tombstone as SECONDARY_IDENTIFIER, which would have deleted a record policy keeps. | Labels are mapped to ids through data-catalog link details. The id prefix decides system and category; the model's record type is kept only where the prefix is ambiguous (support message vs attachment). This fixed case_07. |
| Intent model step added | 68.75 % (regression) | The first prompt classified follow-ups like "verify my account was deleted" as ACCESS requests. Combined with the conservative AND rule, six genuine deletions became NOT_APPLICABLE. | The prompt now defines deletion follow-ups explicitly. Disagreements are logged in `results.json`. Final: 100 % status accuracy, and exactly the 10 non-deletion requests flagged among the 518. |
| Model quota | n/a | The gemini-2.5-flash free tier allows only 20 requests per day. | Per-model fallback on daily-quota errors. Every validated answer is cached on disk, so runs are reproducible and offline. |

## Design guards against overfitting
- Rules come from `deletion_rules.csv` categories, not from sample answers.
- Tunable windows (link ±45 d; worker actions 6 h before to 15 min after the report) are named constants in `linker.py`.
- Gold is read only by `evaluation/`.
