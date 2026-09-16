"""Cross-app consistency pass. Apps in the same vendor family should share auth and gating.
Outliers are not auto-corrected; they are flagged for the human queue with a reason."""

FAMILIES = {
    "Meta": {"members": ["WhatsApp Business", "Meta Ads", "Threads (Meta)"], "expect_auth": "oauth2",
             "expect_blocker": {"app_review", "partner_gate"}, "note": "Meta Graph API: OAuth2 + App Review for advanced access"},
    "Zoho": {"members": ["Zoho CRM", "Zoho Cliq"], "expect_auth": "oauth2", "note": "Zoho accounts OAuth2 across products"},
    "Salesforce": {"members": ["Salesforce", "Salesforce Commerce Cloud"], "expect_auth": "oauth2"},
    "Google": {"members": ["Google Ads", "NotebookLM"], "expect_auth": "oauth2"},
    "Atlassian": {"members": ["Jira"], "expect_auth": "oauth2"},
}


def consistency_pass(finals: list[dict]) -> list[dict]:
    by_name = {m["name"]: m for m in finals}
    for fam, spec in FAMILIES.items():
        members = [by_name[n] for n in spec["members"] if n in by_name]
        for m in members:
            if spec.get("expect_auth") and spec["expect_auth"] not in m["auth_methods"] and m["access"] != "no_public_api":
                m["needs_human"] = True
                m["needs_human_reasons"].append(f"family check: {fam} products use {spec['expect_auth']}, found {m['auth_methods']}")
            if spec.get("expect_blocker") and m["blocker"] not in spec["expect_blocker"] and m["verdict"] == "green":
                m["needs_human"] = True
                m["needs_human_reasons"].append(f"family check: {fam} products normally need app review; verdict green looks optimistic")
    # global sanity rules
    for m in finals:
        if m["access"] == "no_public_api" and m["api_type"] not in ("none", "cli_only", "mcp_only", "sdk_only"):
            m["needs_human"] = True
            m["needs_human_reasons"].append(f"no_public_api but api_type={m['api_type']}")
        if m["verdict"] == "green" and m["access"] in ("partner_gated", "no_public_api"):
            m["needs_human"] = True
            m["needs_human_reasons"].append("green verdict with gated access")
        if "none" in m["auth_methods"] and m["access"] not in ("no_public_api",):
            m["needs_human"] = True
            m["needs_human_reasons"].append("auth 'none' but access implies an API exists")
    return finals
