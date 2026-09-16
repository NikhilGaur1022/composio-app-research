"""LLM research stage. Runs Claude Code headless (`claude -p`) with web tools and a strict JSON schema.

Two passes, deliberately different in capability so the accuracy delta is real:
  pass 1  - search snippets only (WebSearch), Haiku, 4 turns, no registry context, no quote requirement.
  pass 2  - WebSearch + WebFetch, Sonnet, registry + probe context injected, verbatim quotes required.

Auth: uses the logged-in Claude Code session, or ANTHROPIC_API_KEY if set (claude -p honors both).
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .common import SCHEMA_FILE, read_json

CLAUDE = shutil.which("claude") or "claude"
SCHEMA = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
SCHEMA.pop("$schema", None)

ENUM_GUIDE = """
Field rules (use ONLY these enum values):
- auth_methods: oauth2 | api_key | basic | bearer_token | jwt | other | none. List every documented method for the public API. Use "none" only if there is no public API at all.
- access: how a developer obtains credentials.
    self_serve_free   = anyone can sign up and get API credentials on a free tier / dev sandbox
    self_serve_trial  = free trial exists, then paid
    paid_plan         = API access only on a paid subscription tier
    admin_approval    = credentials exist but need a workspace admin / app review / marketplace approval to be used broadly
    partner_gated     = must contact sales, apply for partner program, or be an approved developer
    no_public_api     = no documented public API
- api_type: rest | graphql | rest_and_graphql | sdk_only | cli_only | mcp_only | none
- api_breadth: small (<50 endpoints) | medium (50-300) | large (>300) | unknown
- mcp.status: official (vendor-published MCP server) | community | none | unknown
- verdict: green (agent toolkit buildable today with self-serve creds) | yellow (buildable with caveats: approval, paid tier, narrow API) | red (blocked)
- blocker: none | partner_gate | no_public_api | app_review | paid_only | cli_not_api | undocumented | enterprise_only | deprecated | other
- evidence: one entry per claim with the exact URL and a VERBATIM quote (<=200 chars) copied from that page. Do not paraphrase quotes. If you cannot find a supporting quote, lower confidence instead of inventing one.
- confidence: 0-1, your honest calibrated probability that auth_methods, access and verdict are all correct.
"""


def _prompt(app: dict, mode: str, context: dict | None) -> str:
    base = f"""You are a product-ops research agent. Research the app below and fill the JSON schema exactly.

App: {app['name']}
Category: {app['category']}
Hint: {app['hint']}
{ENUM_GUIDE}
"""
    if mode == "pass1":
        base += """
Constraints for this pass: you may only use WebSearch (no page fetching). Answer from search snippets and your own knowledge. Keep it fast: at most 3 searches. Put the docs URL you believe is correct in docs_url. Evidence quotes may be short snippets from search results.
"""
    else:
        base += """
Method for this pass (be rigorous):
1. Find the OFFICIAL developer docs. Prefer the vendor's own domain over third parties.
2. WebFetch the authentication page and the "getting started / get API key" page. Read them.
3. Determine access by looking at how credentials are obtained (free dev account? paid tier? app review? partner form?). If the docs are behind a login or say "contact us", that is partner_gated / enterprise_only - report it with the quote.
4. Check for an MCP server: vendor-published (official) vs community. Use the registry hints below.
5. If the "app" is really a CLI/open-source tool with no hosted API (e.g. a GitHub project), say api_type cli_only, blocker cli_not_api, and judge buildability as an agent skill rather than a hosted toolkit.
6. Every non-trivial claim needs a verbatim quote from a page you actually fetched.
Budget: at most 4 searches and 6 fetches.
"""
        if context:
            base += "\nPre-computed context from deterministic lookups (use as leads; verify, do not blindly copy):\n"
            base += json.dumps(context, indent=1)[:6000]
    base += "\nReturn ONLY the JSON object."
    return base


def _context_from(registry: dict, probes: dict) -> dict:
    c = {}
    comp = registry.get("composio", {})
    if comp.get("found"):
        c["composio_catalog"] = {"auth": comp.get("auth"), "tools": comp.get("tools"), "url": comp.get("url"),
                                 "wraps_official_mcp": comp.get("wraps_mcp")}
    else:
        c["composio_catalog"] = "not in Composio catalog"
    ag = registry.get("apis_guru", {})
    if ag.get("found"):
        c["apis_guru_openapi"] = {"endpoints": ag.get("endpoints"), "auth": ag.get("auth"), "spec": ag.get("spec_url")}
    mr = registry.get("mcp_registry", {})
    c["mcp_registry"] = {"status": mr.get("status"), "hits": [h["name"] for h in mr.get("hits", [])[:5]]}
    gh = registry.get("github", {})
    c["github_mcp_repos"] = [f"{i['full_name']} ({i['stars']}★)" for i in gh.get("items", [])[:4]]
    up = probes.get("unauth_probe", {})
    if not up.get("skipped"):
        c["unauth_api_probe"] = {"url": up.get("url"), "status": up.get("status"), "www_authenticate": up.get("www_authenticate"),
                                 "inferred_scheme": up.get("inferred_scheme"), "body": up.get("body_snippet")}
    c["docs_page"] = {"url": probes.get("docs_reachable", {}).get("final_url"), "status": probes.get("docs_reachable", {}).get("status"),
                      "signals": {k: v for k, v in probes.get("docs_signals", {}).items() if v}}
    if probes.get("openapi_discovery", {}).get("found"):
        c["openapi_spec_found_at"] = probes["openapi_discovery"]["found"]
    if probes.get("oauth_discovery", {}).get("found"):
        c["oauth_discovery_doc"] = probes["oauth_discovery"]["found"]
    return c


def run_claude(prompt: str, model: str, tools: str, max_turns: int, timeout: int = 420) -> dict:
    cmd = [CLAUDE, "-p", prompt, "--model", model, "--output-format", "json", "--allowedTools", tools,
           "--max-turns", str(max_turns), "--json-schema", json.dumps(SCHEMA)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "seconds": round(time.time() - t0, 1)}
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"non-json output: {p.stdout[:200]} | {p.stderr[:200]}", "seconds": round(time.time() - t0, 1)}
    so = data.get("structured_output")
    if not so:
        # sometimes the model returns the JSON as text in `result`
        try:
            so = json.loads(data.get("result") or "")
        except Exception:  # noqa: BLE001
            so = None
    return {"ok": bool(so), "result": so, "cost_usd": data.get("total_cost_usd"), "turns": data.get("num_turns"),
            "seconds": round(time.time() - t0, 1), "is_error": data.get("is_error"), "error": None if so else (data.get("result") or "no structured output")[:300]}


def research(app: dict, mode: str, registry: dict | None = None, probes: dict | None = None) -> dict:
    if mode == "pass1":
        return run_claude(_prompt(app, "pass1", None), model="haiku", tools="WebSearch", max_turns=5)
    ctx = _context_from(registry or {}, probes or {})
    return run_claude(_prompt(app, "pass2", ctx), model="sonnet", tools="WebSearch,WebFetch", max_turns=14)
