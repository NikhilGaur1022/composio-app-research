"""Quote-grounding judge. Mechanical, no LLM.

For every evidence item the research pass produced:
  1. fetch the cited URL (cached),
  2. check the quote appears in the page text (normalized, fuzzy-tolerant),
  3. record grounded / not_grounded / unreachable.

An evidence item that is not grounded does not count toward confidence in merge.py. This is what
stops hallucinated citations from surviving into the final table.
"""
import difflib

from .common import fetch, html_to_text, normalize


def _grounded(quote: str, page_text: str) -> tuple[bool, float]:
    q = normalize(quote)
    if not q or len(q) < 12:
        return False, 0.0
    if q in page_text:
        return True, 1.0
    # tolerate small differences (punctuation, smart quotes, ellipses): slide a window over the page
    words = q.split()
    if len(words) < 4:
        return False, 0.0
    # cheap anchor: find the rarest 3-gram of the quote in the page, then compare around it
    best = 0.0
    tokens = page_text.split()
    n = len(words)
    for i in range(0, max(0, len(tokens) - n + 1), max(1, n // 2)):
        window = " ".join(tokens[i:i + n + 4])
        r = difflib.SequenceMatcher(None, q, window[:len(q) + 40]).ratio()
        if r > best:
            best = r
            if best >= 0.9:
                break
    return best >= 0.82, round(best, 3)


def judge(app: dict, pass2: dict) -> dict:
    res = pass2.get("result") or {}
    items = res.get("evidence") or []
    out = {"name": app["name"], "checked": 0, "grounded": 0, "unreachable": 0, "items": []}
    pages = {}
    for ev in items:
        url = (ev.get("url") or "").strip()
        quote = ev.get("quote") or ""
        if url not in pages:
            r = fetch(url, timeout=25)
            pages[url] = {"status": r["status"], "text": normalize(html_to_text(r["text"])) if r["text"] else "", "error": r["error"]}
        pg = pages[url]
        item = {"field": ev.get("field"), "url": url, "quote": quote[:200], "status": pg["status"]}
        if pg["status"] == 0 or not pg["text"]:
            item["verdict"] = "unreachable"
            out["unreachable"] += 1
        else:
            ok, score = _grounded(quote, pg["text"])
            item["verdict"] = "grounded" if ok else "not_grounded"
            item["score"] = score
            out["checked"] += 1
            out["grounded"] += int(ok)
        out["items"].append(item)
    out["grounding_rate"] = round(out["grounded"] / out["checked"], 2) if out["checked"] else None
    # per-field view used by merge
    out["by_field"] = {}
    for it in out["items"]:
        f = it["field"]
        out["by_field"].setdefault(f, []).append(it["verdict"])
    return out
