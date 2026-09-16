"""Merge all signals per app into one final row.

Per field, independent sources vote with weights. Confidence is computed from
(a) cross-source agreement, (b) the judge's quote-grounding rate, (c) the model's own stated confidence.
Rows below threshold, or with conflicting sources, go to the human queue.
"""
from collections import Counter, defaultdict

STANDARD_AUTH = {"oauth2", "api_key", "bearer_token", "basic", "jwt"}
ACCESS_SCORE = {"self_serve_free": 40, "self_serve_trial": 35, "paid_plan": 20, "admin_approval": 22, "partner_gated": 5, "no_public_api": 0}
API_SCORE = {"rest": 30, "graphql": 28, "rest_and_graphql": 30, "sdk_only": 15, "mcp_only": 18, "cli_only": 10, "none": 0}
BREADTH_SCORE = {"large": 15, "medium": 12, "small": 8, "unknown": 5}
BLOCKER_FOR_ACCESS = {"partner_gated": "partner_gate", "no_public_api": "no_public_api", "paid_plan": "paid_only", "admin_approval": "app_review"}


def _vote(votes: list[tuple[str, object, float]]):
    """votes: [(source, value, weight)] -> (winner, agreement 0-1, tally)."""
    tally = defaultdict(float)
    for _, v, w in votes:
        if v in (None, "", [], "unknown"):
            continue
        key = tuple(sorted(v)) if isinstance(v, list) else v
        tally[key] += w
    if not tally:
        return None, 0.0, {}
    winner, wscore = max(tally.items(), key=lambda kv: kv[1])
    total = sum(tally.values())
    return (list(winner) if isinstance(winner, tuple) else winner), round(wscore / total, 2), {str(k): round(v, 2) for k, v in tally.items()}


def _auth_compat(a: list[str]) -> list[str]:
    """apis.guru maps 'Authorization: Bearer <api key>' as bearer/basic; treat those as api_key-compatible."""
    s = set(a)
    if s & {"bearer_token", "basic"} and "oauth2" not in s:
        s = (s - {"bearer_token", "basic"}) | {"api_key"}
    return sorted(s)


def buildability(access, api_type, breadth, auth, mcp_status, blocker):
    """Rule-based verdict; numeric score only ranks apps within a verdict band."""
    score = ACCESS_SCORE.get(access, 10) + API_SCORE.get(api_type, 10) + BREADTH_SCORE.get(breadth, 5)
    score += 10 if set(auth or []) & STANDARD_AUTH else (5 if auth and auth != ["none"] else 0)
    score += 5 if mcp_status == "official" else 3 if mcp_status == "community" else 0
    if access in ("partner_gated", "no_public_api") or api_type == "none" or blocker in ("deprecated", "no_public_api", "partner_gate"):
        verdict = "red"
    elif access in ("paid_plan", "admin_approval") or api_type in ("cli_only", "sdk_only", "mcp_only") or blocker not in ("none",):
        verdict = "yellow"
    else:
        verdict = "green"
    return score, verdict


def merge_app(app, registry, probes, pass1, pass2, judge) -> dict:
    r1 = (pass1 or {}).get("result") or {}
    r2 = (pass2 or {}).get("result") or {}
    comp = (registry or {}).get("composio", {})
    ag = (registry or {}).get("apis_guru", {})
    mr = (registry or {}).get("mcp_registry", {})
    gh = (registry or {}).get("github", {})
    up = (probes or {}).get("unauth_probe", {})
    sig = (probes or {}).get("docs_signals", {})
    by_field = (judge or {}).get("by_field", {})

    def gw(field, w):  # weight of pass2 for a field, discounted if its evidence was not grounded
        vs = by_field.get(field, [])
        if not vs:
            return w * 0.7
        g = vs.count("grounded") / len(vs)
        return w * (0.5 + 0.5 * g)

    reasons = []
    sources = {}

    # ---- auth ----
    votes = []
    if comp.get("found") and comp.get("auth"):
        votes.append(("composio_catalog", comp["auth"], 1.0))
    if ag.get("found") and ag.get("auth"):
        votes.append(("apis_guru_spec", _auth_compat(ag["auth"]), 0.6))
    if up.get("inferred_scheme") in ("basic", "api_key", "oauth2"):
        votes.append(("unauth_probe", [up["inferred_scheme"]], 0.3))
    if r2.get("auth_methods"):
        votes.append(("pass2", sorted(set(r2["auth_methods"])), gw("auth_methods", 1.0)))
    if r1.get("auth_methods"):
        votes.append(("pass1", sorted(set(r1["auth_methods"])), 0.25))
    # union with support: a method survives if its summed source weight >= 0.5 (registries list the primary,
    # the model lists all documented methods; both are right).
    support = defaultdict(float)
    for _, v, w in votes:
        for m in v:
            support[m] += w
    auth = sorted(m for m, w in support.items() if w >= 0.5 and m != "none") or (["none"] if not support else sorted(support, key=support.get, reverse=True)[:1])
    agree_n = sum(1 for _, v, _ in votes if set(v) & set(auth))
    auth_agree = round(agree_n / len(votes), 2) if votes else 0.0
    sources["auth_methods"] = {s: v for s, v, _ in votes}
    if comp.get("found") and comp.get("auth") and not (set(comp["auth"]) & set(auth)):
        reasons.append(f"auth disagrees with Composio catalog ({comp['auth']} vs {auth})")

    # ---- access ----
    votes = []
    if r2.get("access"):
        votes.append(("pass2", r2["access"], gw("access", 1.0)))
    if r1.get("access"):
        votes.append(("pass1", r1["access"], 0.25))
    if sig.get("contact_sales") and not sig.get("free_tier"):
        votes.append(("docs_signal", "partner_gated", 0.15))
    if sig.get("free_tier"):
        votes.append(("docs_signal", "self_serve_free", 0.2))
    access, access_agree, _ = _vote(votes)
    access = access or "no_public_api"
    sources["access"] = {s: v for s, v, _ in votes}
    if r1.get("access") and r2.get("access") and r1["access"] != r2["access"]:
        reasons.append(f"pass1/pass2 disagree on access ({r1['access']} vs {r2['access']})")

    # ---- api_type ----
    votes = []
    if r2.get("api_type"):
        votes.append(("pass2", r2["api_type"], gw("api_type", 1.0)))
    if ag.get("found"):
        votes.append(("apis_guru_spec", "rest", 0.5))
    if r1.get("api_type"):
        votes.append(("pass1", r1["api_type"], 0.25))
    api_type, api_agree, _ = _vote(votes)
    api_type = api_type or "none"
    sources["api_type"] = {s: v for s, v, _ in votes}

    # ---- breadth ----
    votes = []
    if ag.get("endpoints") is not None:
        votes.append(("apis_guru_spec", ag["breadth"], 1.0))
    if comp.get("tools"):
        t = comp["tools"]
        votes.append(("composio_tools", "large" if t > 150 else "medium" if t > 40 else "small", 0.5))
    if r2.get("api_breadth"):
        votes.append(("pass2", r2["api_breadth"], gw("api_breadth", 0.8)))
    if r1.get("api_breadth"):
        votes.append(("pass1", r1["api_breadth"], 0.2))
    breadth, breadth_agree, _ = _vote(votes)
    breadth = breadth or "unknown"
    sources["api_breadth"] = {s: v for s, v, _ in votes}

    # ---- mcp ----
    votes = []
    if mr.get("status") and mr["status"] != "none":
        votes.append(("mcp_registry", mr["status"], 0.8))
    if comp.get("wraps_mcp"):
        votes.append(("composio_wraps_mcp", "official", 0.7))
    top = (gh.get("items") or [None])[0]
    if top and top["stars"] >= 20:
        votes.append(("github", "community", 0.4))
    if (r2.get("mcp") or {}).get("status") and r2["mcp"]["status"] != "unknown":
        votes.append(("pass2", r2["mcp"]["status"], gw("mcp", 0.8)))
    if (r1.get("mcp") or {}).get("status") and r1["mcp"]["status"] != "unknown":
        votes.append(("pass1", r1["mcp"]["status"], 0.2))
    mcp_status, mcp_agree, _ = _vote(votes)
    mcp_status = mcp_status or "none"
    # 'official' beats 'community' if any strong source says official
    if any(v == "official" and w >= 0.7 for _, v, w in votes):
        mcp_status = "official"
    mcp_url = (r2.get("mcp") or {}).get("url") or (mr.get("official") or [{}])[0].get("repo") or (top or {}).get("url") or (r1.get("mcp") or {}).get("url")
    sources["mcp"] = {s: v for s, v, _ in votes}

    # ---- blocker & verdict ----
    blocker = r2.get("blocker") or r1.get("blocker") or "none"
    if blocker == "none" and access in BLOCKER_FOR_ACCESS:
        blocker = BLOCKER_FOR_ACCESS[access]
    if api_type == "cli_only":
        blocker = "cli_not_api"
    if api_type == "none":
        blocker = "no_public_api"
    score, verdict = buildability(access, api_type, breadth, auth, mcp_status, blocker)
    model_verdict = r2.get("verdict")
    if model_verdict and model_verdict != verdict:
        reasons.append(f"formula verdict {verdict} != model verdict {model_verdict}")

    # ---- confidence ----
    grounding = (judge or {}).get("grounding_rate")
    unreachable = (judge or {}).get("unreachable", 0)
    checked = (judge or {}).get("checked", 0)
    self_conf = r2.get("confidence") if r2 else (r1.get("confidence", 0.3) * 0.6 if r1 else 0.2)
    agreement = (auth_agree + access_agree + api_agree) / 3
    g = grounding if grounding is not None else 0.4
    confidence = round(0.40 * agreement + 0.35 * g + 0.25 * (self_conf or 0.3), 2)
    if not r2:
        confidence = min(confidence, 0.45)
        reasons.append("pass2 failed; relying on pass1 + registries")
    if grounding is not None and grounding < 0.5:
        reasons.append(f"only {int(grounding * 100)}% of quotes grounded in cited pages")
    if checked and unreachable > checked:
        reasons.append("most cited pages unreachable")
    if access in ("partner_gated", "no_public_api") and confidence < 0.8:
        reasons.append("gated/no-API finding needs a human eye")
    needs_human = confidence < 0.6 or bool(reasons)

    evidence = []
    for ev in (r2.get("evidence") or []):
        v = next((it["verdict"] for it in (judge or {}).get("items", []) if it["url"] == ev.get("url") and it["quote"][:60] == (ev.get("quote") or "")[:60]), "unchecked")
        evidence.append({**ev, "grounded": v})
    evidence.sort(key=lambda e: {"grounded": 0, "unchecked": 1, "unreachable": 2, "not_grounded": 3}[e["grounded"]])
    if comp.get("found"):
        evidence.append({"field": "auth_methods", "url": comp.get("url"), "quote": f"Composio catalog: auth {comp.get('auth_raw')}, {comp.get('tools')} tools", "grounded": "registry"})
    if ag.get("found"):
        evidence.append({"field": "api_breadth", "url": ag.get("spec_url"), "quote": f"APIs.guru OpenAPI spec: {ag.get('endpoints')} operations, securitySchemes {ag.get('auth')}", "grounded": "registry"})
    if up.get("status") in (401, 403):
        evidence.append({"field": "auth_methods", "url": up.get("url"), "quote": f"Unauthenticated probe -> HTTP {up['status']}; WWW-Authenticate: {up.get('www_authenticate')}; body: {up.get('body_snippet')}", "grounded": "probe"})

    return {
        "id": app["id"], "name": app["name"], "category": app["category"], "hint": app["hint"],
        "one_liner": r2.get("one_liner") or r1.get("one_liner") or "",
        "auth_methods": auth, "access": access, "api_type": api_type, "api_breadth": breadth,
        "docs_url": r2.get("docs_url") or r1.get("docs_url") or "",
        "mcp": {"status": mcp_status, "url": mcp_url}, "verdict": verdict, "score": score, "blocker": blocker,
        "model_verdict": model_verdict, "confidence": confidence, "agreement": round(agreement, 2), "grounding_rate": grounding,
        "needs_human": needs_human, "needs_human_reasons": reasons, "sources": sources, "evidence": evidence,
        "composio": {"in_catalog": bool(comp.get("found")), "slug": comp.get("slug"), "tools": comp.get("tools"),
                     "auth": comp.get("auth"), "auth_agrees": (bool(set(comp.get("auth") or []) & set(auth)) if comp.get("found") else None),
                     "url": comp.get("url")},
        "apis_guru": {"found": bool(ag.get("found")), "endpoints": ag.get("endpoints")},
        "probe": {"status": up.get("status"), "www_authenticate": up.get("www_authenticate"), "inferred": up.get("inferred_scheme")},
        "notes": r2.get("notes") or r1.get("notes") or "",
        "pass1": {k: r1.get(k) for k in ("auth_methods", "access", "api_type", "api_breadth", "verdict", "blocker")} if r1 else None,
        "pass2": {k: r2.get(k) for k in ("auth_methods", "access", "api_type", "api_breadth", "verdict", "blocker", "confidence")} if r2 else None,
        "cost_usd": round((pass1 or {}).get("cost_usd") or 0, 3) + round((pass2 or {}).get("cost_usd") or 0, 3),
    }


def summarize(finals):
    c = Counter()
    for m in finals:
        c[("verdict", m["verdict"])] += 1
        c[("access", m["access"])] += 1
        c[("human", m["needs_human"])] += 1
        for a in m["auth_methods"]:
            c[("auth", a)] += 1
    print("[merge] summary:")
    for k, v in sorted(c.items()):
        print(f"   {k[0]:8} {str(k[1]):20} {v}")
    print(f"   total LLM cost ${sum(m['cost_usd'] for m in finals):.2f}")
