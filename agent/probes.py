"""Empirical probes: verify by behaviour, not by reading.

  - docs_reachable: does the hinted docs URL resolve (200)?
  - openapi_discovery: does a public OpenAPI/Swagger spec exist at common paths?
  - oidc_discovery: /.well-known/openid-configuration or /oauth/authorize present -> OAuth2 confirmed
  - unauth_401: hit a likely API base unauthenticated and read WWW-Authenticate / error body
"""
import json
import re

from .common import fetch, html_to_text

# hand-curated API base URLs to probe unauthenticated (public knowledge, cheap to verify)
API_BASES = {
    "Salesforce": "https://login.salesforce.com/services/oauth2/token", "HubSpot": "https://api.hubapi.com/crm/v3/objects/contacts",
    "Pipedrive": "https://api.pipedrive.com/v1/deals", "Attio": "https://api.attio.com/v2/objects", "Twenty": "https://api.twenty.com/rest/companies",
    "Podio": "https://api.podio.com/org/", "Zoho CRM": "https://www.zohoapis.com/crm/v2/Leads", "Close": "https://api.close.com/api/v1/lead/",
    "Copper": "https://api.copper.com/developer_api/v1/people", "DealCloud": "https://api.docs.dealcloud.com",
    "Zendesk": "https://support.zendesk.com/api/v2/tickets.json", "Intercom": "https://api.intercom.io/contacts",
    "Freshdesk": "https://freshdesk.com/api/v2/tickets", "Front": "https://api2.frontapp.com/conversations",
    "Pylon": "https://api.usepylon.com/issues", "LiveAgent": "https://www.liveagent.com/api/v3/tickets",
    "Plain": "https://core-api.uk.plain.com/graphql/v1", "Help Scout": "https://api.helpscout.net/v2/conversations",
    "Gorgias": "https://api.gorgias.com/api/tickets", "Gladly": "https://api.gladly.com/api/v1/customers",
    "Slack": "https://slack.com/api/conversations.list", "Twilio": "https://api.twilio.com/2010-04-01/Accounts.json",
    "Zoho Cliq": "https://cliq.zoho.com/api/v2/channels", "Lark (Larksuite)": "https://open.larksuite.com/open-apis/im/v1/messages",
    "Pumble": "https://pumble-api-keys.addons.marketplace.cake.com/listChannels", "Discord": "https://discord.com/api/v10/users/@me",
    "Telegram": "https://api.telegram.org/botTOKEN/getMe", "WhatsApp Business": "https://graph.facebook.com/v21.0/me/messages",
    "Aircall": "https://api.aircall.io/v1/calls", "Vonage": "https://api.nexmo.com/account/get-balance",
    "Google Ads": "https://googleads.googleapis.com/v18/customers:listAccessibleCustomers", "Meta Ads": "https://graph.facebook.com/v21.0/me/adaccounts",
    "LinkedIn Ads": "https://api.linkedin.com/rest/adAccounts", "GoHighLevel": "https://services.leadconnectorhq.com/contacts/",
    "Mailchimp": "https://us1.api.mailchimp.com/3.0/lists", "Klaviyo": "https://a.klaviyo.com/api/profiles/",
    "systeme.io": "https://api.systeme.io/api/contacts", "Pinterest": "https://api.pinterest.com/v5/user_account",
    "Threads (Meta)": "https://graph.threads.net/v1.0/me", "SendGrid": "https://api.sendgrid.com/v3/templates",
    "Shopify": "https://shopify.dev/docs/api/admin-rest", "WooCommerce": "https://woocommerce.com/wp-json/wc/v3/products",
    "BigCommerce": "https://api.bigcommerce.com/stores/x/v3/catalog/products", "Salesforce Commerce Cloud": "https://account.demandware.com/dwsso/oauth2/access_token",
    "Magento (Adobe Commerce)": "https://developer.adobe.com/commerce/webapi/rest/", "Squarespace": "https://api.squarespace.com/1.0/commerce/orders",
    "Ecwid": "https://app.ecwid.com/api/v3/1/products", "Gumroad": "https://api.gumroad.com/v2/products",
    "Amazon Selling Partner": "https://sellingpartnerapi-na.amazon.com/orders/v0/orders", "fanbasis": "https://fanbasis.com/api",
    "DataForSEO": "https://api.dataforseo.com/v3/appendix/user_data", "SE Ranking": "https://api4.seranking.com/sites",
    "Ahrefs": "https://api.ahrefs.com/v3/site-explorer/metrics", "MrScraper": "https://app.mrscraper.com/api/scrapers",
    "Apify": "https://api.apify.com/v2/acts", "Firecrawl": "https://api.firecrawl.dev/v1/scrape",
    "Bright Data": "https://api.brightdata.com/zone", "Sherlock": "https://github.com/sherlock-project/sherlock",
    "Waterfall.io": "https://api.waterfall.io/v1", "Clay": "https://api.clay.com/v3/sources",
    "GitHub": "https://api.github.com/user", "Vercel": "https://api.vercel.com/v9/projects", "Netlify": "https://api.netlify.com/api/v1/sites",
    "Cloudflare": "https://api.cloudflare.com/client/v4/zones", "Supabase": "https://api.supabase.com/v1/projects",
    "Neo4j": "https://api.neo4j.io/v1/instances", "Snowflake": "https://docs.snowflake.com/en/developer-guide/sql-api/index",
    "MongoDB Atlas": "https://cloud.mongodb.com/api/atlas/v2/groups", "Datadog": "https://api.datadoghq.com/api/v1/validate",
    "Sentry": "https://sentry.io/api/0/projects/", "Notion": "https://api.notion.com/v1/users", "Airtable": "https://api.airtable.com/v0/meta/bases",
    "Linear": "https://api.linear.app/graphql", "Jira": "https://api.atlassian.com/oauth/token/accessible-resources",
    "Asana": "https://app.asana.com/api/1.0/users/me", "Monday.com": "https://api.monday.com/v2", "ClickUp": "https://api.clickup.com/api/v2/team",
    "Coda": "https://coda.io/apis/v1/docs", "Smartsheet": "https://api.smartsheet.com/2.0/sheets", "Harvest": "https://api.harvestapp.com/v2/users/me",
    "Stripe": "https://api.stripe.com/v1/customers", "Plaid": "https://production.plaid.com/accounts/get", "Binance": "https://api.binance.com/api/v3/account",
    "Paygent Connect": "https://paygent.com", "iPayX": "https://ipayx.ai/docs", "QuickBooks": "https://quickbooks.api.intuit.com/v3/company/1/companyinfo/1",
    "Xero": "https://api.xero.com/api.xro/2.0/Organisation", "Brex": "https://platform.brexapis.com/v2/transactions",
    "Ramp": "https://api.ramp.com/developer/v1/transactions", "PitchBook": "https://api.pitchbook.com/",
    "NotebookLM": "https://notebooklm.google.com", "Otter AI": "https://otter.ai/api", "Fathom": "https://api.fathom.ai/external/v1/meetings",
    "Consensus": "https://consensus.app/api", "Reducto": "https://platform.reducto.ai/parse", "Devin": "https://api.devin.ai/v1/sessions",
    "higgsfield": "https://higgsfield.ai/cli", "Mermaid CLI": "https://github.com/mermaid-js/mermaid-cli",
    "YouTube Transcript": "https://transcriptapi.com/api/v2/youtube/transcript", "Grain": "https://api.grain.com/_/public-api/recordings",
}

OPENAPI_PATHS = ["/openapi.json", "/swagger.json", "/api-docs", "/v1/openapi.json", "/openapi.yaml", "/swagger/v1/swagger.json"]


def _hint_url(hint: str) -> str:
    h = hint.split(" (")[0].split()[0]
    return h if h.startswith("http") else "https://" + h


def probe(app: dict) -> dict:
    name = app["name"]
    out = {}

    # 1. docs reachability
    hu = _hint_url(app["hint"])
    r = fetch(hu)
    out["docs_reachable"] = {"url": hu, "status": r["status"], "final_url": r["final_url"], "error": r["error"]}
    txt = html_to_text(r["text"])[:20000].lower() if r["text"] else ""
    # cheap keyword signals from the hinted page
    out["docs_signals"] = {
        "oauth": bool(re.search(r"\boauth ?2?(\.0)?\b", txt)),
        "api_key": bool(re.search(r"\bapi[ -]?keys?\b", txt)),
        "bearer": "bearer" in txt,
        "basic_auth": "basic auth" in txt,
        "graphql": "graphql" in txt,
        "rest": bool(re.search(r"\brest(ful)?\b", txt)),
        "contact_sales": bool(re.search(r"contact (sales|us)|request (access|a demo)|book a demo|talk to sales", txt)),
        "free_tier": bool(re.search(r"\bfree (tier|plan|trial|account)\b|sign ?up for free|get started for free", txt)),
        "enterprise_only": bool(re.search(r"enterprise (plan|customers|only)", txt)),
        "mcp": bool(re.search(r"\bmcp\b|model context protocol", txt)),
    }

    # 2. unauth probe on API base
    base = API_BASES.get(name)
    if base and not base.startswith("https://github.com") and "docs" not in base.split("//")[1].split("/")[0]:
        r2 = fetch(base, allow_redirects=False)
        body = (r2["text"] or "")[:400]
        www = r2["headers"].get("www-authenticate")
        scheme = None
        if www:
            w = www.lower()
            scheme = "bearer_token" if "bearer" in w else "basic" if "basic" in w else "other"
        elif r2["status"] in (401, 403):
            bl = body.lower()
            if "oauth" in bl:
                scheme = "oauth2"
            elif "api key" in bl or "api_key" in bl or "apikey" in bl or "x-api-key" in bl:
                scheme = "api_key"
            elif "bearer" in bl or "token" in bl:
                scheme = "bearer_token"
            elif "basic" in bl:
                scheme = "basic"
        out["unauth_probe"] = {"url": base, "status": r2["status"], "www_authenticate": www,
                               "body_snippet": body.replace("\n", " ")[:200], "inferred_scheme": scheme,
                               "api_alive": r2["status"] in (400, 401, 403, 405, 422)}
    else:
        out["unauth_probe"] = {"url": base, "skipped": True}

    # 3. OpenAPI discovery at the API base origin
    found = None
    if base:
        origin = "/".join(base.split("/")[:3])
        for p in OPENAPI_PATHS:
            r3 = fetch(origin + p, timeout=10)
            if r3["status"] == 200 and r3["text"][:2000].lstrip().startswith(("{", "openapi", "swagger")):
                found = origin + p
                break
    out["openapi_discovery"] = {"found": found}

    # 4. OIDC / OAuth discovery
    oidc = None
    if base:
        origin = "/".join(base.split("/")[:3])
        for p in ["/.well-known/openid-configuration", "/.well-known/oauth-authorization-server"]:
            r4 = fetch(origin + p, timeout=10)
            if r4["status"] == 200 and '"authorization_endpoint"' in r4["text"][:5000]:
                oidc = origin + p
                break
    out["oauth_discovery"] = {"found": oidc}
    return out
