"""CSV loading and identity reconstruction."""
import csv
import hashlib
import os
from collections import defaultdict
from datetime import datetime

from evidence import classify, norm_phone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(ROOT, "dataset")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def read(name):
    return list(csv.DictReader(open(os.path.join(DATASET, name), encoding="utf-8")))


def sha(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


class Identities:
    """user_id -> every identifier the person has ever held (current snapshot + audit history)."""

    def __init__(self):
        self.users = {}
        self.history = defaultdict(list)  # user_id -> audit rows
        self.by_value = defaultdict(set)  # normalized identifier -> user_ids
        for u in read("user_identities.csv"):
            self.users[u["user_id"]] = u
        for a in read("user_audit_log.csv"):
            self.history[a["user_id"]].append(a)
        self.ids = {uid: self._collect(uid) for uid in self.users}
        for uid, vals in self.ids.items():
            for v in vals:
                self.by_value[v].add(uid)

    def _collect(self, uid):
        u = self.users[uid]
        vals = {uid}
        raw = [u["email"], u["phone"], u["email_hash"]]
        for a in self.history[uid]:
            raw += [a["old_value"], a["new_value"]]
        for v in raw:
            c = classify(v or "")
            if c:
                vals.add(c[1])
                if c[0] == "email":
                    vals.add(sha(c[1]))
        if u["name"]:
            vals.add("name:" + u["name"].strip().lower())
        return vals

    def resolve(self, email):
        e = email.strip().lower()
        hits = self.by_value.get(e, set())
        # prefer the user whose *current* email matches, then a historical holder
        cur = [uid for uid in hits if self.users[uid]["email"].strip().lower() == e]
        if cur:
            return sorted(cur)[0]
        return sorted(hits)[0] if hits else None

    def emails(self, uid):
        return {v for v in self.ids[uid] if "@" in v}
