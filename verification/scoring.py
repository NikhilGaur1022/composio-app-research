"""Score every stage against the hand-labelled gold set.

Stages scored: pass1 (raw), pass2 (raw), merged (registries+probes+judge+voting), final (merged + human corrections).
Outputs verification/scores.json with per-field accuracy, per-app hit/miss table and a confidence calibration table.

Run: python -m verification.scoring
"""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["auth_methods", "access", "api_type", "mcp", "verdict"]


def _slug(name):
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _load(stage, name):
    p = ROOT / "results" / stage / f"{_slug(name)}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    if stage in ("pass1", "pass2"):
        return d.get("result")
    return d


def _get(row, field):
    if row is None:
        return None
    if field == "mcp":
        m = row.get("mcp")
        return m.get("status") if isinstance(m, dict) else m
    return row.get(field)


def _correct(field, pred, gold_row):
    gold = gold_row[field]
    accept = set((gold_row.get("accept") or {}).get(field, []))
    if pred is None:
        return False
    if field == "auth_methods":
        p = set(pred if isinstance(pred, list) else [pred])
        g = set(gold) | accept
        if gold == ["none"]:
            return p == {"none"} or not p
        return bool(p & g) and p != {"none"}
    if field == "mcp":
        return (pred == "official") == (gold == "official") or pred in accept
    return pred == gold or pred in accept


def apply_human(merged, changes):
    """changes: [{name, field, from, to, reason, evidence}] -> new dict with changes applied."""
    out = json.loads(json.dumps(merged))
    for ch in changes:
        if ch["name"] != out["name"]:
            continue
        if ch["field"] == "mcp":
            out["mcp"]["status"] = ch["to"]
        else:
            out[ch["field"]] = ch["to"]
    # verdict is derived, so recompute it after corrections (same rule as the page)
    import sys
    sys.path.insert(0, str(ROOT))
    from agent.merge import buildability
    out["score"], out["verdict"] = buildability(out["access"], out["api_type"], out["api_breadth"], out["auth_methods"], out["mcp"]["status"], out["blocker"])
    return out


def main():
    gold = json.loads((ROOT / "verification" / "gold.json").read_text(encoding="utf-8"))["gold"]
    changes = json.loads((ROOT / "verification" / "human_changes.json").read_text(encoding="utf-8")) if (ROOT / "verification" / "human_changes.json").exists() else []
    stages = ["pass1", "pass2", "merged", "final"]
    totals = {s: defaultdict(lambda: [0, 0]) for s in stages}
    rows = []
    calib = defaultdict(lambda: [0, 0])  # bucket -> [correct_all, n]
    for g in gold:
        r = {"name": g["name"], "gold": {f: g[f] for f in FIELDS}, "stages": {}}
        merged = _load("final", g["name"])
        preds = {"pass1": _load("pass1", g["name"]), "pass2": _load("pass2", g["name"]), "merged": merged,
                 "final": apply_human(merged, changes) if merged else None}
        for s in stages:
            row = preds[s]
            r["stages"][s] = {}
            for f in FIELDS:
                pred = _get(row, f)
                ok = _correct(f, pred, g)
                r["stages"][s][f] = {"pred": pred, "ok": ok}
                totals[s][f][0] += int(ok)
                totals[s][f][1] += 1
        if merged:
            conf = merged.get("confidence", 0)
            b = "<0.6" if conf < 0.6 else "0.6-0.8" if conf < 0.8 else ">=0.8"
            all_ok = all(r["stages"]["merged"][f]["ok"] for f in ("auth_methods", "access", "verdict"))
            calib[b][0] += int(all_ok)
            calib[b][1] += 1
            r["confidence"] = conf
            r["needs_human"] = merged.get("needs_human")
        rows.append(r)

    summary = {}
    for s in stages:
        per = {f: {"correct": c, "n": n, "acc": round(c / n, 3) if n else None} for f, (c, n) in totals[s].items()}
        c = sum(v[0] for v in totals[s].values()); n = sum(v[1] for v in totals[s].values())
        per["_overall"] = {"correct": c, "n": n, "acc": round(c / n, 3) if n else None}
        summary[s] = per
    out = {"fields": FIELDS, "n_gold": len(gold), "summary": summary, "rows": rows,
           "calibration": {b: {"all_core_fields_correct": c, "n": n, "rate": round(c / n, 2) if n else None} for b, (c, n) in calib.items()},
           "human_changes": len(changes)}
    (ROOT / "verification" / "scores.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"gold n={len(gold)}")
    for s in stages:
        print(f"  {s:7} overall {summary[s]['_overall']['acc']}  " + "  ".join(f"{f}={summary[s][f]['acc']}" for f in FIELDS))
    print("  calibration:", dict(out["calibration"]))


if __name__ == "__main__":
    main()
