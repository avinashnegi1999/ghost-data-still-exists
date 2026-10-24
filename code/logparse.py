"""Line parser for the six log dialects found in dataset/evidence/logs."""
import json
import re

KV = re.compile(r'([A-Za-z_][\w.]*)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')
BRACKET = re.compile(r'^(\S+) \[(\w+)\] \[([^\]]+)\] \[([^\]]+)\] (\w+): ?(.*)$')
SYSLOG = re.compile(r'^(\S+) <\d+>\d+ \S+ (\S+) (\S+) - - (.*)$')


def _kv(text):
    out = {}
    for k, q, b in KV.findall(text):
        out[k] = q.replace('\\"', '"') if b == "" else b
    return out


def parse_line(line):
    """Return {ts, service, trace, event, kv} or None for blank lines."""
    line = line.rstrip("\n")
    if not line.strip():
        return None
    ts, _, rest = line.partition(" ")
    rest = rest.strip()
    if rest.startswith("{"):
        try:
            kv = {k: v if isinstance(v, str) else json.dumps(v) for k, v in json.loads(rest).items()}
        except ValueError:
            kv = _kv(rest)
        return dict(ts=ts, service=kv.get("service") or kv.get("logger"), trace=kv.get("trace_id"),
                    event=kv.get("event"), kv=kv)
    m = BRACKET.match(line)
    if m:
        ts, _lvl, svc, trace, event, tail = m.groups()
        return dict(ts=ts, service=svc, trace=trace, event=event, kv=_kv(tail))
    if " | " in rest:
        parts = [p.strip() for p in rest.split(" | ", 4)]
        if len(parts) == 5:
            _lvl, svc, trace, event, tail = parts
            return dict(ts=ts, service=svc, trace=trace, event=event, kv=_kv(tail))
    m = SYSLOG.match(line)
    if m:
        ts, svc, trace, tail = m.groups()
        kv = _kv(tail)
        return dict(ts=ts, service=svc, trace=trace, event=kv.get("event"), kv=kv)
    kv = _kv(rest)
    if "message" in kv and "function" in kv:  # cloud runtime: kv pairs nested inside message="..."
        inner = _kv(kv["message"])
        return dict(ts=ts, service=kv["function"], trace=kv.get("requestId"), event=inner.get("event"), kv=inner)
    return dict(ts=ts, service=kv.get("service"), trace=kv.get("trace_id"), event=kv.get("event"), kv=kv)
