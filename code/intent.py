"""Decide whether a request text actually authorizes deletion.

Primary: a model reads each distinct text (any language) and returns a strict JSON verdict.
Guardrail: a multilingual keyword check runs on every text. A request is audited as a deletion only
when the model and the keywords both say so; when the model is unavailable the keywords decide alone.
Wrongly running a deletion on an access, export, correction or marketing request destroys data the
person wanted kept, so disagreement resolves to NOT_APPLICABLE and is listed in the evaluation report.
"""
import re

import llm

TYPES = ["DELETION", "ACCESS", "EXPORT", "CORRECTION", "MARKETING_OPT_OUT", "ACCOUNT_SUPPORT", "OTHER"]

PROMPT = """Classify customer privacy requests sent to an online shop. For each numbered request decide its
primary type: one of {types}. authorizes_deletion is true only when the person asks for their account or
personal data to be deleted, erased, removed or permanently closed. This includes follow-ups on a deletion the
person already asked for: asking to check, audit, verify or confirm that their account/data is really gone, or
to remove copies that remain, authorizes deletion (type DELETION). Requests that only ask to stop marketing,
correct details, get a copy/download/export of data, ask what data is held, or get login help do NOT authorize
deletion, even if they mention the account. Requests may be in any language.

Requests:
{items}"""

SCHEMA = {"type": "OBJECT", "properties": {"results": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "INTEGER"},
    "request_type": {"type": "STRING", "enum": TYPES},
    "authorizes_deletion": {"type": "BOOLEAN"}},
    "required": ["id", "request_type", "authorizes_deletion"]}}}, "required": ["results"]}

DELETE_WORDS = re.compile(
    r"delet|eras|remov|close|gone|purge|forget|"
    r"supprim|suppression|effac|elimin|borr|exclu|apag|lösch|entfern|cancell|"
    r"삭제|削除|消去|删除|刪除|удал|हटा|मिटा", re.I)
# the person says they want the account kept, or explicitly says this is not a deletion request
KEEP_ACCOUNT = re.compile(
    r"not (asking for|a request to) \w*|do not (close|delete)|don't (close|delete)|without removing|"
    r"leave (the|my) (account|profile)|keep the account|continue using|still (want|need|use)|"
    r"marketing lists only", re.I)


def keyword_verdict(text):
    """Deterministic fallback: a deletion verb and no statement that the account must stay."""
    return bool(DELETE_WORDS.search(text)) and not KEEP_ACCOUNT.search(text)


class IntentClassifier:
    def __init__(self, texts, batch=40):
        self.verdict = {}
        self.disagreements = []
        uniq = sorted(set(texts))
        model = {}
        for i in range(0, len(uniq), batch):
            chunk = uniq[i:i + batch]
            items = "\n".join(f"{j}. {t}" for j, t in enumerate(chunk))

            def validate(obj, n=len(chunk)):
                ids = sorted(r["id"] for r in obj["results"])
                if ids != list(range(n)):
                    raise ValueError("model did not classify every request")

            try:
                res = llm.generate_json("intent_classification", PROMPT.format(types=", ".join(TYPES), items=items),
                                        SCHEMA, validate)
                for r in res["results"]:
                    model[chunk[r["id"]]] = r
            except RuntimeError as e:
                print("WARN intent model unavailable, keyword fallback:", e)
        for t in uniq:
            kw = keyword_verdict(t)
            m = model.get(t)
            if m is None:
                self.verdict[t] = kw
            else:
                if m["authorizes_deletion"] != kw:
                    self.disagreements.append((t, m, kw))
                # deletion runs only when both agree it was asked for (conservative on disagreement)
                self.verdict[t] = m["authorizes_deletion"] and kw

    def __call__(self, case):
        return self.verdict.get(case["request_text"], keyword_verdict(case["request_text"]))
