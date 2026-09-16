"""Deterministic, zero-LLM registry lookups.

Sources:
  1. Composio toolkit catalog  (SDK if COMPOSIO_API_KEY is valid, else public docs index)
  2. APIs.guru OpenAPI directory (auth schemes + exact endpoint count)
  3. Official MCP registry (registry.modelcontextprotocol.io)
  4. GitHub search (official/community MCP servers)
"""
import json
import os
import re
import subprocess
from urllib.parse import urlparse

from .common import cached, fetch, slug

COMPOSIO_INDEX_URL = "https://docs.composio.dev/toolkits.md"
COMPOSIO_TK_URL = "https://docs.composio.dev/toolkits/{slug}.md"

# app name -> Composio toolkit slug (public catalog). None = not in catalog.
# Hand-mapped once from the sitemap slug list; the agent records this as evidence.
COMPOSIO_SLUGS = {
    "Salesforce": "salesforce", "HubSpot": "hubspot", "Pipedrive": "pipedrive", "Attio": "attio",
    "Twenty": "twenty", "Podio": None, "Zoho CRM": "zoho", "Close": "close", "Copper": None,
    "DealCloud": None,
    "Zendesk": "zendesk", "Intercom": "intercom", "Freshdesk": "freshdesk", "Front": None,
    "Pylon": "pylon_mcp", "LiveAgent": None, "Plain": "plain", "Help Scout": "help_scout",
    "Gorgias": "gorgias", "Gladly": None,
    "Slack": "slack", "Twilio": None, "Zoho Cliq": None, "Lark (Larksuite)": None,
    "Pumble": "pumble", "Discord": "discord", "Telegram": "telegram", "WhatsApp Business": "whatsapp",
    "Aircall": None, "Vonage": None,
    "Google Ads": "googleads", "Meta Ads": "metaads", "LinkedIn Ads": "linkedin_ads",
    "GoHighLevel": "highlevel", "Mailchimp": "mailchimp", "Klaviyo": "klaviyo", "systeme.io": None,
    "Pinterest": "pinterest", "Threads (Meta)": None, "SendGrid": "sendgrid",
    "Shopify": "shopify", "WooCommerce": None, "BigCommerce": None,
    "Salesforce Commerce Cloud": None, "Magento (Adobe Commerce)": None, "Squarespace": None,
    "Ecwid": None, "Gumroad": "gumroad", "Amazon Selling Partner": None, "fanbasis": None,
    "DataForSEO": "dataforseo", "SE Ranking": None, "Ahrefs": "ahrefs", "MrScraper": "mrscraper",
    "Apify": "apify", "Firecrawl": "firecrawl", "Bright Data": "brightdata", "Sherlock": None,
    "Waterfall.io": None, "Clay": "clay_mcp",
    "GitHub": "github", "Vercel": "vercel", "Netlify": "netlify_mcp", "Cloudflare": "cloudflare",
    "Supabase": "supabase", "Neo4j": "neo4j", "Snowflake": "snowflake", "MongoDB Atlas": None,
    "Datadog": "datadog", "Sentry": "sentry",
    "Notion": "notion", "Airtable": "airtable", "Linear": "linear", "Jira": "jira", "Asana": "asana",
    "Monday.com": "monday", "ClickUp": "clickup", "Coda": "coda", "Smartsheet": None, "Harvest": "harvest",
    "Stripe": "stripe", "Plaid": "plaid_mcp", "Binance": None, "Paygent Connect": None, "iPayX": None,
    "QuickBooks": "quickbooks", "Xero": "xero", "Brex": "brex", "Ramp": "ramp", "PitchBook": None,
    "NotebookLM": None, "Otter AI": "otter_ai_mcp", "Fathom": "fathom", "Consensus": "consensus",
    "Reducto": None, "Devin": "devin_mcp", "higgsfield": "higgsfield_mcp", "Mermaid CLI": "mermaid_chart_mcp",
    "YouTube Transcript": "youtube_transcript", "Grain": None,
}

AUTH_MAP = {"OAUTH2": "oauth2", "OAUTH1": "other", "API_KEY": "api_key", "BEARER_TOKEN": "bearer_token",
            "BASIC": "basic", "BASIC_WITH_JWT": "jwt", "JWT": "jwt", "NO_AUTH": "none", "COMPOSIO_LINK": "other",
            "GOOGLE_SERVICE_ACCOUNT": "other", "CALCOM_AUTH": "other", "SNOWFLAKE": "other", "BILLCOM_AUTH": "other",
            "CUSTOM": "other"}


# ---------- 1. Composio ----------
def _composio_index() -> dict:
    """Parse the public toolkit index table -> {slug_lower: {name, tools, triggers, auth[], managed}}."""
    def _do():
        r = fetch(COMPOSIO_INDEX_URL)
        out = {}
        for line in r["text"].splitlines():
            if not line.startswith("| [") or "](/toolkits/" not in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 6:
                continue
            m = re.match(r"\[(.+?)\]\(/toolkits/(.+?)\.md\)", cells[0])
            if not m:
                continue
            name, s = m.group(1), m.group(2)
            auth = [a.strip() for a in cells[4].replace(",", "/").split("/") if a.strip() and a.strip() != "—"]
            out[s.lower()] = {"name": name, "slug": cells[1].strip("`"), "tools": _int(cells[2]),
                              "triggers": _int(cells[3]), "auth_raw": auth,
                              "auth": sorted({AUTH_MAP.get(a.upper(), "other") for a in auth}),
                              "managed_oauth": cells[5] not in ("—", "", "No")}
        return out
    return cached("registry", "composio_index", _do, ttl_days=7)


def _int(s):
    try:
        return int(s)
    except ValueError:
        return None


def composio_lookup(app_name: str) -> dict:
    """Try SDK first (needs valid key); fall back to public index. Never raises."""
    s = COMPOSIO_SLUGS.get(app_name)
    res = {"source": "composio", "slug": s, "found": False, "via": None, "url": None}
    if not s:
        res["note"] = "not in Composio catalog (checked sitemap slug list)"
        return res
    key = os.environ.get("COMPOSIO_API_KEY")
    if key:
        try:
            from composio import Composio  # noqa: WPS433
            tk = Composio(api_key=key).toolkits.get(s)
            auth = [getattr(a, "mode", "") for a in (getattr(tk, "auth_config_details", None) or [])]
            res.update(found=True, via="sdk", name=getattr(tk, "name", s), auth_raw=auth,
                       auth=sorted({AUTH_MAP.get(a.upper(), "other") for a in auth}),
                       tools=getattr(getattr(tk, "meta", None), "tools_count", None),
                       url=f"https://docs.composio.dev/toolkits/{s}")
            return res
        except Exception as e:  # noqa: BLE001
            res["sdk_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    idx = _composio_index()
    row = idx.get(s.lower())
    if row:
        res.update(found=True, via="public_index", url=f"https://docs.composio.dev/toolkits/{s}", **row)
        res["wraps_mcp"] = s.endswith("_mcp")
    else:
        res["note"] = f"slug '{s}' not found in public index"
    return res


# ---------- 2. APIs.guru ----------
def _apis_guru_list() -> dict:
    return cached("registry", "apis_guru_list", lambda: json.loads(fetch("https://api.apis.guru/v2/list.json")["text"] or "{}"), ttl_days=7)


APIS_GURU_KEYS = {
    "Slack": "slack.com", "Twilio": "twilio.com:api", "GitHub": "github.com", "Stripe": "stripe.com",
    "SendGrid": "sendgrid.com", "Zendesk": "zendesk.com", "HubSpot": "hubspot.com:crm",
    "Datadog": "datadoghq.com", "Vercel": "vercel.com", "Netlify": "netlify.com", "Shopify": "shopify.com",
    "Intercom": "intercom.io", "Asana": "asana.com", "Jira": "atlassian.com:jira", "Notion": "notion.com",
    "Plaid": "plaid.com", "Xero": "xero.com:xero_accounting", "Pipedrive": "pipedrive.com", "Mailchimp": "mailchimp.com",
    "Snowflake": "snowflake.com", "Linear": "linear.app", "Cloudflare": "cloudflare.com", "Discord": "discord.com",
    "Apify": "apify.com", "Bright Data": "brightdata.com", "Vonage": "nexmo.com", "Freshdesk": "freshdesk.com",
    "Sentry": "sentry.io", "Klaviyo": "klaviyo.com", "Squarespace": "squarespace.com", "Ecwid": "ecwid.com",
    "BigCommerce": "bigcommerce.com", "Amazon Selling Partner": "amazonaws.com:sp-api", "Airtable": "airtable.com",
    "Monday.com": "monday.com", "ClickUp": "clickup.com", "Coda": "coda.io", "Smartsheet": "smartsheet.com",
    "Harvest": "harvestapp.com", "QuickBooks": "intuit.com", "Brex": "brex.com", "Ramp": "ramp.com",
    "Close": "close.com", "Copper": "copper.com", "Front": "frontapp.com", "Help Scout": "helpscout.com",
    "Gorgias": "gorgias.com", "Aircall": "aircall.io", "Telegram": "telegram.org", "Pinterest": "pinterest.com",
    "WooCommerce": "woocommerce.com", "Gumroad": "gumroad.com", "DataForSEO": "dataforseo.com", "Firecrawl": "firecrawl.dev",
    "Supabase": "supabase.com", "Neo4j": "neo4j.com", "MongoDB Atlas": "mongodb.com", "Zoho CRM": "zoho.com",
    "Salesforce": "salesforce.com", "Podio": "podio.com", "Binance": "binance.com", "Twenty": "twenty.com",
}


def apis_guru_lookup(app_name: str, hint: str) -> dict:
    lst = _apis_guru_list()
    res = {"source": "apis_guru", "found": False}
    if not lst:
        res["note"] = "apis.guru unreachable"
        return res
    # exact hand key, else search by domain fragment
    keys = []
    k = APIS_GURU_KEYS.get(app_name)
    if k and k in lst:
        keys = [k]
    else:
        dom = re.sub(r"^(www\.|docs\.|developers?\.|api\.|open\.)", "", hint.split()[0].split("/")[0]).lower()
        base = dom.split(".")[0]
        if len(base) >= 4:
            keys = [x for x in lst if x.split(":")[0].startswith(base) or f".{base}." in f".{x.split(':')[0]}."]
    if not keys:
        return res
    key = keys[0]
    pref = lst[key]["preferred"]
    ver = lst[key]["versions"][pref]
    spec_url = ver.get("swaggerUrl") or ver.get("openapiVer")
    res.update(found=True, key=key, version=pref, spec_url=spec_url, title=ver.get("info", {}).get("title"),
               url=f"https://apis.guru/?q={key.split(':')[0]}")
    # count endpoints + auth schemes from the spec (cached)
    try:
        spec = json.loads(fetch(spec_url, timeout=40)["text"] or "{}")
        paths = spec.get("paths", {})
        ops = sum(1 for p in paths.values() for m in p if m.lower() in ("get", "post", "put", "patch", "delete"))
        schemes = spec.get("components", {}).get("securitySchemes") or spec.get("securityDefinitions") or {}
        auth = set()
        for sch in schemes.values():
            t = (sch.get("type") or "").lower()
            if t == "oauth2":
                auth.add("oauth2")
            elif t == "apikey":
                auth.add("api_key")
            elif t == "http":
                auth.add("basic" if (sch.get("scheme") or "").lower() == "basic" else "bearer_token")
            elif t == "openidconnect":
                auth.add("oauth2")
        res.update(endpoints=ops, auth=sorted(auth), breadth="small" if ops < 50 else "medium" if ops <= 300 else "large")
    except Exception as e:  # noqa: BLE001
        res["spec_error"] = str(e)[:120]
    return res


# ---------- 3. Official MCP registry ----------
def mcp_registry_lookup(app_name: str) -> dict:
    q = app_name.split(" (")[0].split(".")[0].split(" ")[0].lower()
    if app_name in ("Help Scout", "Google Ads", "Meta Ads", "LinkedIn Ads", "Bright Data", "Zoho CRM", "Zoho Cliq",
                    "Salesforce Commerce Cloud", "Amazon Selling Partner", "MongoDB Atlas", "YouTube Transcript", "Mermaid CLI"):
        q = app_name.replace(" (Meta)", "").lower()

    def _do():
        r = fetch(f"https://registry.modelcontextprotocol.io/v0/servers?search={q}&limit=20")
        try:
            data = json.loads(r["text"] or "{}")
        except Exception:  # noqa: BLE001
            return {"servers": [], "error": r.get("error") or r["status"]}
        servers = []
        for it in data.get("servers", []):
            srv = it.get("server", it)
            name = srv.get("name", "")
            servers.append({"name": name, "description": (srv.get("description") or "")[:140],
                            "repo": (srv.get("repository") or {}).get("url"),
                            "status": (it.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {}) or {}).get("status")})
        return {"servers": servers}
    data = cached("registry", f"mcpreg {q}", _do, ttl_days=7)
    servers = data.get("servers", [])
    # official = namespace matches vendor domain (io.github.<vendor> is community)
    ql = q.replace(" ", "")
    hits = [s for s in servers if ql in s["name"].lower().replace("-", "").replace("_", "")]
    official = [s for s in hits if not s["name"].startswith("io.github.")
                and s["name"].split("/")[0].lower().split(".")[-1] in (ql, ql + "ai", ql + "app", ql + "io", ql + "hq")]
    return {"source": "mcp_registry", "query": q, "url": f"https://registry.modelcontextprotocol.io/v0/servers?search={q}",
            "hits": hits[:8], "official": official[:3],
            "status": "official" if official else "community" if hits else "none"}


# ---------- 4. GitHub ----------
def github_mcp_search(app_name: str) -> dict:
    q = app_name.replace(" (Meta)", "").replace(" (Larksuite)", "").replace(" (Adobe Commerce)", "")
    def _do():
        try:
            out = subprocess.run(["gh", "api", "-X", "GET", "search/repositories", "-f", f"q={q} mcp server in:name,description",
                                  "-f", "sort=stars", "-f", "per_page=8"], capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                return {"error": out.stderr[:200], "items": []}
            data = json.loads(out.stdout)
            return {"items": [{"full_name": i["full_name"], "stars": i["stargazers_count"], "url": i["html_url"],
                               "desc": (i.get("description") or "")[:120]} for i in data.get("items", [])]}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200], "items": []}
    data = cached("registry", f"gh mcp {q}", _do, ttl_days=7)
    return {"source": "github", "query": q, "url": f"https://github.com/search?q={q.replace(' ', '+')}+mcp+server&type=repositories", **data}


def lookup_all(app: dict) -> dict:
    return {
        "composio": composio_lookup(app["name"]),
        "apis_guru": apis_guru_lookup(app["name"], app["hint"]),
        "mcp_registry": mcp_registry_lookup(app["name"]),
        "github": github_mcp_search(app["name"]),
    }
