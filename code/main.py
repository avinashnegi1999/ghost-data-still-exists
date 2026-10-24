"""Run the deletion audit for every request in dataset/deletion_requests.csv and write ../output.csv.

Usage:  python code/main.py            (uses cached model answers; GEMINI_API_KEY needed only for cache misses)
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data  # noqa: E402
import intent  # noqa: E402
import llm  # noqa: E402
import pipeline  # noqa: E402
import vision  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def build_auditor(texts):
    activity = data.read("system_activity.csv")
    media = vision.media_rows(data.DATASET, data.read("evidence_files.csv"), vision.display_index(activity))
    return pipeline.Auditor(media_rows=media, intent=intent.IntentClassifier(texts))


def usage_report(path, n_requests):
    u = llm.USAGE
    total = u["input_tokens"] + u["output_tokens"]
    cost = llm.cost(u["input_tokens"], u["output_tokens"])
    lines = ["# Usage report", "",
             "Final full-dataset run that produced `output.csv` (%d requests)." % n_requests, "",
             "Cached replays count the tokens of the original live call, so these figures are the real cost "
             "of producing the answers.", "",
             "| Provider | Model | Purpose | Calls | Cache replays | Input tokens | Output tokens | Est. cost (USD) |",
             "|---|---|---|---|---|---|---|---|"]
    per_model = {}
    for (model, purpose), p in sorted(u["by_purpose"].items()):
        lines.append("| Google | %s | %s | %d | %d | %d | %d | %.4f |" % (
            model, purpose, p["calls"], p["cache_hits"], p["input_tokens"], p["output_tokens"],
            llm.cost(p["input_tokens"], p["output_tokens"])))
        m = per_model.setdefault(model, [0, 0, 0, 0])
        for i, f in enumerate(("calls", "cache_hits", "input_tokens", "output_tokens")):
            m[i] += p[f]
    for model, m in sorted(per_model.items()):
        lines.append("| Google | **%s total** | all | %d | %d | %d | %d | %.4f |" % (model, *m, llm.cost(m[2], m[3])))
    lines.append("| **Overall** | all models | all | %d | %d | %d | %d | %.4f |" % (
        u["calls"], u["cache_hits"], u["input_tokens"], u["output_tokens"], cost))
    lines += ["", "- Total tokens: %d" % total,
              "- Average tokens per request: %.1f" % (total / n_requests),
              "- Estimated total cost: $%.4f" % cost,
              "- Estimated cost per request: $%.6f" % (cost / n_requests),
              "- Pricing assumption: $%.2f / 1M input, $%.2f / 1M output (thinking tokens billed as output)."
              % (llm.PRICE_IN, llm.PRICE_OUT) + " Same flash-tier list price applied to every model used.",
              "- Model fallback: calls start on %s; on a daily-quota error they move to the next model in %s."
              % (llm.MODEL, llm.FALLBACK_MODELS)]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    cases = data.read("deletion_requests.csv")
    samples = data.read("sample_deletion_requests.csv")
    auditor = build_auditor([c["request_text"] for c in cases + samples])
    results = auditor.run(cases, samples + cases)
    out = os.path.join(data.ROOT, "output.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for cid in sorted(results, key=lambda c: int(c.split("_")[1])):
            status, steps, _ = results[cid]
            w.writerow([cid, status, pipeline.dumps(steps)])
    usage_report(os.path.join(HERE, "evaluation", "usage_report.md"), len(cases))
    counts = {}
    for s, _, _ in results.values():
        counts[s] = counts.get(s, 0) + 1
    print("wrote", out, len(results), "rows", counts)


if __name__ == "__main__":
    main()
