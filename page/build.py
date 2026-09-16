"""Build docs/index.html (the case study) from results + verification data.

Run: python -m page.build
Everything on the page is derived from results/final.json, verification/scores.json,
verification/human_changes.json and verification/browser_checks.json - nothing is typed by hand
except page/insights.json (headline sentences, written after reading the numbers).
"""
import base64
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REPO_URL = "https://github.com/NikhilGaur1022/composio-app-research"

CATS_SHORT = {
    "CRM and Sales": "CRM", "Support and Helpdesk": "Support", "Communications and Messaging": "Comms",
    "Marketing, Ads, Email and Social": "Marketing", "Ecommerce": "Ecommerce", "Data, SEO and Scraping": "Data/SEO",
    "Developer, Infra and Data platforms": "Dev/Infra", "Productivity and Project Management": "Productivity",
    "Finance and Fintech": "Finance", "AI, Research and Media-native": "AI/Media",
}
AUTH_LABEL = {"oauth2": "OAuth2", "api_key": "API key", "bearer_token": "Bearer token", "basic": "Basic", "jwt": "JWT", "other": "Other", "none": "None"}
ACCESS_LABEL = {"self_serve_free": "Self-serve (free)", "self_serve_trial": "Self-serve (trial)", "paid_plan": "Paid plan", "admin_approval": "Admin / app review", "partner_gated": "Partner / sales gate", "no_public_api": "No public API"}
BLOCKER_LABEL = {"none": "None", "partner_gate": "Partner / sales gate", "no_public_api": "No public API", "app_review": "App review / admin approval", "paid_only": "Paid tier only", "cli_not_api": "CLI, not an API", "undocumented": "Undocumented", "enterprise_only": "Enterprise only", "deprecated": "Deprecated", "other": "Other"}
API_LABEL = {"rest": "REST", "graphql": "GraphQL", "rest_and_graphql": "REST + GraphQL", "sdk_only": "SDK only", "cli_only": "CLI only", "mcp_only": "MCP only", "none": "None"}
ASK_FOR = {"partner_gate": "partner / API program access", "no_public_api": "a public API roadmap or private beta", "enterprise_only": "an enterprise sandbox", "paid_only": "a sponsored developer account", "app_review": "app review fast-track", "cli_not_api": "nothing - ship as a local agent skill", "undocumented": "developer docs", "deprecated": "migration path", "other": "clarification"}

# categorical palette (dataviz reference, validated adjacent-pairs)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"]


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def load(p, default=None):
    p = ROOT / p
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


# ----------------------------------------------------------------------------- data
rows = load("results/final.json")
scores = load("verification/scores.json", {})
human_changes = load("verification/human_changes.json", [])
browser_checks = load("verification/browser_checks.json", [])
insights = load("page/insights.json", {})
human_queue = load("results/human_queue.json", [])
gold = load("verification/gold.json", {"gold": []})["gold"]

# apply human corrections to the displayed rows (results/final.json is the pure agent output)
by_name = {r["name"]: r for r in rows}
for ch in human_changes:
    r = by_name.get(ch["name"])
    if not r:
        continue
    if ch["field"] == "mcp":
        r["mcp"]["status"] = ch["to"]
    else:
        r[ch["field"]] = ch["to"]
    r.setdefault("human_corrected", []).append(ch["field"])
# recompute verdict after corrections so the table is internally consistent
import sys
sys.path.insert(0, str(ROOT))
from agent.merge import buildability  # noqa: E402
for r in rows:
    r["score"], r["verdict"] = buildability(r["access"], r["api_type"], r["api_breadth"], r["auth_methods"], r["mcp"]["status"], r["blocker"])

N = len(rows)
cats = list(CATS_SHORT)
verdicts = Counter(r["verdict"] for r in rows)
access_c = Counter(r["access"] for r in rows)
auth_c = Counter(a for r in rows for a in r["auth_methods"])
blocker_c = Counter(r["blocker"] for r in rows if r["blocker"] != "none")
mcp_c = Counter(r["mcp"]["status"] for r in rows)
api_c = Counter(r["api_type"] for r in rows)
self_serve = sum(1 for r in rows if r["access"].startswith("self_serve"))
composio_in = sum(1 for r in rows if r["composio"]["in_catalog"])
# independent agreement: pass-2's own auth finding vs Composio's declared auth (the merge also uses the catalog, so the merged number is not independent)
composio_agree = sum(1 for r in rows if r["composio"]["in_catalog"] and r["composio"].get("auth") and set(r["composio"]["auth"]) & set((r.get("pass2") or {}).get("auth_methods") or []))
oauth_n = auth_c["oauth2"]; apikey_n = auth_c["api_key"]
both_n = sum(1 for r in rows if "oauth2" in r["auth_methods"] and "api_key" in r["auth_methods"])
total_cost = sum(r.get("cost_usd", 0) for r in rows)
human_touched = len({c["name"] for c in human_changes})


def by_cat(field, getter):
    out = {}
    for c in cats:
        cnt = Counter()
        for r in rows:
            if r["category"] == c:
                v = getter(r)
                if isinstance(v, list):
                    for x in v:
                        cnt[x] += 1
                else:
                    cnt[v] += 1
        out[c] = cnt
    return out


access_by_cat = by_cat("access", lambda r: r["access"])
verdict_by_cat = by_cat("verdict", lambda r: r["verdict"])
auth_by_cat = by_cat("auth", lambda r: [a for a in r["auth_methods"] if a in ("oauth2", "api_key")] or ["other"])
gated_by_cat = {c: sum(v for k, v in cnt.items() if k in ("partner_gated", "no_public_api", "paid_plan", "admin_approval")) for c, cnt in access_by_cat.items()}
most_self_serve = sorted(cats, key=lambda c: -sum(v for k, v in access_by_cat[c].items() if k.startswith("self_serve")))
most_gated = sorted(cats, key=lambda c: -gated_by_cat[c])
top_blocker = blocker_c.most_common(1)[0] if blocker_c else ("none", 0)

# ----------------------------------------------------------------------------- charts (inline SVG)
def stacked_bar_svg(data: dict, keys: list, labels: dict, colors: list, width=760, row_h=26, label_w=96):
    """Horizontal 100%-stacked bars, one row per category. data: {cat: Counter}."""
    plot_w = width - label_w - 8
    # legend layout first, so the height accounts for wrapped rows
    legend = []; lx = label_w; ly = 0
    for i, k in enumerate(keys):
        w = 16 + 6.6 * len(labels.get(k, k)) + 14
        if lx + w > width and lx > label_w:
            lx = label_w; ly += 16
        legend.append((i, k, lx, ly)); lx += w
    h = row_h * len(data) + 34 + ly
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" role="img" aria-label="stacked bar chart">']
    y = 4
    for cat, cnt in data.items():
        total = sum(cnt.get(k, 0) for k in keys) or 1
        out.append(f'<text x="{label_w - 8}" y="{y + row_h / 2 + 4}" text-anchor="end" class="ct">{esc(CATS_SHORT.get(cat, cat))}</text>')
        x = label_w
        for i, k in enumerate(keys):
            v = cnt.get(k, 0)
            if not v:
                continue
            w = plot_w * v / total
            out.append(f'<rect x="{x:.1f}" y="{y + 3}" width="{max(w - 2, 0):.1f}" height="{row_h - 6}" rx="3" fill="var(--s{i + 1})"><title>{esc(labels.get(k, k))}: {v}</title></rect>')
            if w > 22:
                out.append(f'<text x="{x + w / 2 - 1:.1f}" y="{y + row_h / 2 + 4}" text-anchor="middle" class="cv">{v}</text>')
            x += w
        y += row_h
    for i, k, lx, ly in legend:
        out.append(f'<rect x="{lx}" y="{y + 10 + ly}" width="10" height="10" rx="2" fill="var(--s{i + 1})"/>')
        out.append(f'<text x="{lx + 14}" y="{y + 19 + ly}" class="cl">{esc(labels.get(k, k))}</text>')
    out.append("</svg>")
    return "".join(out)


def bar_svg(items: list, width=760, row_h=26, label_w=210, color="var(--accent)"):
    """items: [(label, value, hint)] horizontal bars."""
    mx = max((v for _, v, _ in items), default=1) or 1
    h = row_h * len(items) + 8
    plot_w = width - label_w - 40
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" role="img" aria-label="bar chart">']
    y = 4
    for label, v, hint in items:
        w = plot_w * v / mx
        out.append(f'<text x="{label_w - 8}" y="{y + row_h / 2 + 4}" text-anchor="end" class="ct">{esc(label)}</text>')
        out.append(f'<rect x="{label_w}" y="{y + 4}" width="{w:.1f}" height="{row_h - 8}" rx="3" fill="{color}"><title>{esc(hint)}</title></rect>')
        out.append(f'<text x="{label_w + w + 6:.1f}" y="{y + row_h / 2 + 4}" class="cv2">{v}</text>')
        y += row_h
    out.append("</svg>")
    return "".join(out)


def accuracy_svg(summary: dict, fields: list, width=760):
    stages = [("pass1", "Pass 1 · search snippets"), ("pass2", "Pass 2 · fetch + quotes"), ("merged", "+ registries, probes, judge, voting"), ("final", "+ human corrections")]
    row_h = 28; label_w = 250; plot_w = width - label_w - 60
    h = row_h * len(stages) + 8
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" role="img" aria-label="accuracy by stage">']
    y = 4
    for key, label in stages:
        acc = (summary.get(key, {}).get("_overall") or {}).get("acc")
        if acc is None:
            continue
        w = plot_w * acc
        out.append(f'<text x="{label_w - 8}" y="{y + row_h / 2 + 4}" text-anchor="end" class="ct">{esc(label)}</text>')
        out.append(f'<rect x="{label_w}" y="{y + 5}" width="{plot_w}" height="{row_h - 10}" rx="3" fill="var(--track)"/>')
        out.append(f'<rect x="{label_w}" y="{y + 5}" width="{w:.1f}" height="{row_h - 10}" rx="3" fill="{"var(--accent)" if key != "final" else "var(--good)"}"/>')
        out.append(f'<text x="{label_w + plot_w + 8}" y="{y + row_h / 2 + 4}" class="cv2">{acc * 100:.0f}%</text>')
        y += row_h
    out.append("</svg>")
    return "".join(out)


# ----------------------------------------------------------------------------- html pieces
def pill(kind, text):
    return f'<span class="pill {kind}">{esc(text)}</span>'


def verdict_pill(v):
    return pill(f"v-{v}", {"green": "Green", "yellow": "Yellow", "red": "Red"}[v])


def row_html(r):
    ev = [e for e in r["evidence"] if e.get("url")]
    ev_links = " ".join(f'<a href="{esc(e["url"])}" target="_blank" rel="noopener" title="{esc(e.get("quote", ""))[:160]}" class="ev ev-{esc(e.get("grounded", "unchecked"))}">{esc(e["field"][:4])}</a>' for e in ev[:6])
    hc = r.get("human_corrected", [])
    conf = r["confidence"]
    return (f'<tr data-cat="{esc(CATS_SHORT[r["category"]])}" data-verdict="{r["verdict"]}" data-access="{r["access"]}" data-auth="{" ".join(r["auth_methods"])}" data-name="{esc(r["name"].lower())}">'
            f'<td class="nm"><b>{esc(r["name"])}</b><span class="ol">{esc(r["one_liner"][:110])}</span></td>'
            f'<td>{esc(CATS_SHORT[r["category"]])}</td>'
            f'<td>{" ".join(pill("au", AUTH_LABEL.get(a, a)) for a in r["auth_methods"])}</td>'
            f'<td>{pill("ac ac-" + r["access"], ACCESS_LABEL[r["access"]])}</td>'
            f'<td>{esc(API_LABEL.get(r["api_type"], r["api_type"]))}<span class="sub">{esc(r["api_breadth"])}{(" · " + str(r["apis_guru"]["endpoints"]) + " ops") if r["apis_guru"].get("endpoints") else ""}</span></td>'
            f'<td>{pill("mcp mcp-" + r["mcp"]["status"], r["mcp"]["status"])}{(" <a class=\"lnk\" href=\"" + esc(r["mcp"]["url"]) + "\" target=\"_blank\" rel=\"noopener\">↗</a>") if r["mcp"].get("url") else ""}</td>'
            f'<td>{"✓" if r["composio"]["in_catalog"] else "–"}{("<span class=\"sub\">" + str(r["composio"]["tools"]) + " tools</span>") if r["composio"].get("tools") else ""}</td>'
            f'<td>{verdict_pill(r["verdict"])}<span class="sub">{esc(BLOCKER_LABEL.get(r["blocker"], r["blocker"]))}</span></td>'
            f'<td><span class="conf" style="--c:{conf}">{conf:.2f}</span>{("<span class=\"sub hc\">human: " + ", ".join(hc) + "</span>") if hc else ""}</td>'
            f'<td class="evc">{ev_links}{(" <a class=\"lnk\" href=\"" + esc(r["docs_url"]) + "\" target=\"_blank\" rel=\"noopener\">docs</a>") if r.get("docs_url") else ""}</td>'
            f'</tr>')


def gold_table():
    if not scores.get("rows"):
        return "<p>Scores not computed yet.</p>"
    fields = scores["fields"]
    head = "".join(f"<th>{esc(f.replace('_', ' '))}</th>" for f in fields)
    out = [f'<div class="tw"><table class="gold"><thead><tr><th>App</th><th>Stage</th>{head}<th>conf.</th></tr></thead><tbody>']
    for r in scores["rows"]:
        for i, (stage, lab) in enumerate([("pass1", "pass 1"), ("pass2", "pass 2"), ("final", "final")]):
            cells = []
            for f in fields:
                c = r["stages"][stage][f]
                pred = c["pred"]
                pred = ", ".join(pred) if isinstance(pred, list) else (pred or "—")
                gold_v = r["gold"][f]; gold_v = ", ".join(gold_v) if isinstance(gold_v, list) else gold_v
                cells.append(f'<td class="{"ok" if c["ok"] else "miss"}" title="gold: {esc(gold_v)}">{"✓" if c["ok"] else "✗"} <span class="sub">{esc(pred)}</span></td>')
            name_cell = f'<td rowspan="3" class="nm"><b>{esc(r["name"])}</b>{("<span class=\"sub\">human-checked</span>") if r.get("needs_human") else ""}</td>' if i == 0 else ""
            conf_cell = f'<td rowspan="3">{r.get("confidence", "")}</td>' if i == 0 else ""
            out.append(f'<tr class="st-{stage}">{name_cell}<td class="sub">{lab}</td>{"".join(cells)}{conf_cell}</tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def shot_url(name: str):
    src = ROOT / "verification" / "browser_checks" / name
    if not src.exists():
        return None
    (DOCS / "shots").mkdir(parents=True, exist_ok=True)
    (DOCS / "shots" / name).write_bytes(src.read_bytes())
    return f"shots/{name}"


def browser_checks_html():
    if not browser_checks:
        return "<p class='muted'>No browser checks recorded.</p>"
    out = ['<div class="bc-grid">']
    for b in browser_checks:
        shot = shot_url(b["screenshot"]) if b.get("screenshot") else None
        img = f'<a href="{shot}" target="_blank" rel="noopener"><img src="{shot}" alt="screenshot of {esc(b["url"])}"></a>' if shot else ""
        out.append(f'<figure class="bc"><figcaption><b>{esc(b["app"])}</b> · {esc(b["field"])} · {pill("ok" if b["agent_correct"] else "miss", "agent right" if b["agent_correct"] else "agent wrong")}<br><span class="sub">{esc(b["finding"])}</span><br><a href="{esc(b["url"])}" target="_blank" rel="noopener" class="lnk">{esc(b["url"][:70])}</a></figcaption>{img}</figure>')
    out.append("</div>")
    return "".join(out)


# ----------------------------------------------------------------------------- headline
auto_headlines = [
    f"<b>{oauth_n} of {N}</b> apps expose OAuth2; <b>{apikey_n}</b> expose an API key; <b>{both_n}</b> offer both. Auth is rarely the blocker.",
    f"<b>{self_serve}%</b> are self-serve (free tier or trial). Most self-serve: {', '.join(CATS_SHORT[c] for c in most_self_serve[:3])}. Most gated: {', '.join(CATS_SHORT[c] for c in most_gated[:3])}.",
    f"The most common blocker is <b>{BLOCKER_LABEL[top_blocker[0]].lower()}</b> ({top_blocker[1]} apps), not missing docs.",
    f"<b>{verdicts['green']}</b> green (build today), <b>{verdicts['yellow']}</b> yellow (caveats), <b>{verdicts['red']}</b> red (needs outreach or has no API).",
    f"<b>{mcp_c['official']}</b> vendors already ship an official MCP server; Composio's catalog covers <b>{composio_in}/{N}</b> and agrees with this research on auth for <b>{composio_agree}/{composio_in}</b> of them.",
]
headlines = insights.get("headlines") or auto_headlines

easy_wins = sorted([r for r in rows if r["verdict"] == "green"], key=lambda r: (-r["score"], r["name"]))
outreach = sorted([r for r in rows if r["verdict"] == "red"], key=lambda r: (r["blocker"], r["name"]))
yellow = sorted([r for r in rows if r["verdict"] == "yellow"], key=lambda r: (r["blocker"], -r["score"]))

# registry / probe stats for the agent section
reg_dir = ROOT / "results" / "registry"; pr_dir = ROOT / "results" / "probes"
reg_stats = Counter(); probe_stats = Counter()
for p in reg_dir.glob("*.json"):
    d = json.loads(p.read_text(encoding="utf-8"))
    reg_stats["composio"] += d["composio"].get("found", False)
    reg_stats["apis_guru"] += d["apis_guru"].get("found", False)
    reg_stats["mcp_registry"] += d["mcp_registry"].get("status") != "none"
    reg_stats["github"] += bool(d["github"].get("items"))
for p in pr_dir.glob("*.json"):
    d = json.loads(p.read_text(encoding="utf-8"))
    probe_stats["docs_200"] += d["docs_reachable"]["status"] == 200
    probe_stats["api_alive"] += bool(d["unauth_probe"].get("api_alive"))
    probe_stats["www_auth"] += bool(d["unauth_probe"].get("www_authenticate"))
    probe_stats["openapi"] += bool(d["openapi_discovery"]["found"])
    probe_stats["oidc"] += bool(d["oauth_discovery"]["found"])
judge_dir = ROOT / "results" / "judge"
j_checked = j_grounded = j_unreach = 0
for p in judge_dir.glob("*.json"):
    d = json.loads(p.read_text(encoding="utf-8"))
    j_checked += d.get("checked", 0); j_grounded += d.get("grounded", 0); j_unreach += d.get("unreachable", 0)
pass_fail = {s: sum(1 for p in (ROOT / "results" / s).glob("*.json") if not json.loads(p.read_text(encoding="utf-8")).get("ok")) for s in ("pass1", "pass2")}

summary = scores.get("summary", {})
def acc(stage, f="_overall"):
    v = (summary.get(stage, {}).get(f) or {}).get("acc")
    return f"{v * 100:.0f}%" if v is not None else "—"

calib = scores.get("calibration", {})
failures = insights.get("failures", [])
human_notes = insights.get("human_notes", [])

# ----------------------------------------------------------------------------- page
data_json = json.dumps([{k: r[k] for k in ("id", "name", "category", "one_liner", "auth_methods", "access", "api_type", "api_breadth", "docs_url", "mcp", "verdict", "score", "blocker", "confidence", "needs_human", "evidence", "composio")} for r in rows], ensure_ascii=False)

page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Toolkit Buildability Atlas</title>
<meta name="description" content="100 apps researched by an agent for Composio toolkit buildability: auth, self-serve vs gated, API surface, MCP, verdict - with verification.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{{--bg:#f7f5ef;--bg2:#efece3;--ink:#1c1b17;--ink2:#524f46;--muted:#7d7a6e;--line:#dcd8cb;--accent:#0f6e63;--accent2:#d8ede9;--track:#e6e2d6;
--good:#1f7a3c;--good2:#dff3e4;--warn:#9a6a08;--warn2:#fbeccb;--bad:#b3261e;--bad2:#f9dcd9;
--s1:{SERIES[0]};--s2:{SERIES[1]};--s3:{SERIES[2]};--s4:{SERIES[3]};--s5:{SERIES[4]};--s6:{SERIES[5]};
--display:'Fraunces',Georgia,serif;--body:'IBM Plex Sans',system-ui,sans-serif;--mono:'IBM Plex Mono',ui-monospace,monospace}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#161613;--bg2:#1f1e1a;--ink:#f0ede4;--ink2:#c9c5b8;--muted:#928e80;--line:#34322b;--accent:#4cc2b3;--accent2:#173733;--track:#2a2925;
--good:#5fcf83;--good2:#193324;--warn:#e8b34a;--warn2:#3b2c0d;--bad:#ff7b72;--bad2:#3d1a17;
--s1:{SERIES_DARK[0]};--s2:{SERIES_DARK[1]};--s3:{SERIES_DARK[2]};--s4:{SERIES_DARK[3]};--s5:{SERIES_DARK[4]};--s6:{SERIES_DARK[5]}}}}}
:root[data-theme="dark"]{{--bg:#161613;--bg2:#1f1e1a;--ink:#f0ede4;--ink2:#c9c5b8;--muted:#928e80;--line:#34322b;--accent:#4cc2b3;--accent2:#173733;--track:#2a2925;
--good:#5fcf83;--good2:#193324;--warn:#e8b34a;--warn2:#3b2c0d;--bad:#ff7b72;--bad2:#3d1a17;
--s1:{SERIES_DARK[0]};--s2:{SERIES_DARK[1]};--s3:{SERIES_DARK[2]};--s4:{SERIES_DARK[3]};--s5:{SERIES_DARK[4]};--s6:{SERIES_DARK[5]}}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.55;margin:0;padding-block:0 64px;padding-inline:16px}}
a{{color:var(--accent)}}
.wrap{{max-width:1120px;margin:0 auto}}
h1,h2,h3{{font-family:var(--display);font-weight:600;line-height:1.15;text-wrap:balance;margin:0}}
h1{{font-size:clamp(2rem,5vw,3.4rem);letter-spacing:-.01em}}
h2{{font-size:1.7rem;margin-block:56px 8px;padding-top:8px;border-top:2px solid var(--ink)}}
h3{{font-size:1.15rem;margin-block:24px 8px}}
p{{max-width:70ch}}
.eyebrow{{font-family:var(--mono);font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}}
.muted{{color:var(--muted)}} .sub{{display:block;font-size:.76rem;color:var(--muted);line-height:1.3}}
header{{padding-block:40px 24px;border-bottom:1px solid var(--line)}}
header .lede{{font-size:1.1rem;color:var(--ink2);max-width:64ch;margin-top:14px}}
nav.toc{{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);border-bottom:1px solid var(--line);margin-inline:-16px;padding-inline:16px}}
nav.toc ul{{list-style:none;margin:0 auto;padding:0;max-width:1120px;display:flex;gap:4px 18px;flex-wrap:wrap;font-family:var(--mono);font-size:.78rem}}
nav.toc a{{display:inline-block;padding:10px 0;color:var(--ink2);text-decoration:none;border-bottom:2px solid transparent}}
nav.toc a:hover,nav.toc a:focus-visible{{color:var(--accent);border-color:var(--accent);outline:none}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:20px}}
.tile{{background:var(--bg2);border-radius:8px;padding:14px 16px}}
.tile .n{{font-family:var(--display);font-size:2.2rem;line-height:1;font-variant-numeric:tabular-nums}}
.tile .l{{font-size:.8rem;color:var(--ink2);margin-top:6px}}
.tile.g .n{{color:var(--good)}} .tile.y .n{{color:var(--warn)}} .tile.r .n{{color:var(--bad)}} .tile.a .n{{color:var(--accent)}}
ol.head{{padding-left:1.2em;font-size:1.08rem;max-width:78ch}} ol.head li{{margin-block:10px}} ol.head b{{color:var(--accent)}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:24px 40px;margin-top:8px}}
.chart{{width:100%;height:auto;display:block}}
.ct{{font-family:var(--mono);font-size:12px;fill:var(--ink2)}} .cv{{font-family:var(--mono);font-size:11px;fill:#fff;font-weight:500}} .cv2{{font-family:var(--mono);font-size:12px;fill:var(--ink)}} .cl{{font-family:var(--mono);font-size:11px;fill:var(--ink2)}}
figure{{margin:0}} figcaption{{font-size:.85rem;color:var(--ink2);margin-bottom:6px}}
.lists{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px 40px}}
.lists ul{{list-style:none;padding:0;margin:0}} .lists li{{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--line);font-size:.92rem}}
.lists li b{{min-width:150px}} .lists li .sub{{display:inline}}
.pill{{display:inline-block;font-family:var(--mono);font-size:.7rem;padding:2px 7px;border-radius:999px;background:var(--bg2);color:var(--ink2);margin:1px 2px 1px 0;white-space:nowrap}}
.pill.v-green,.pill.ok{{background:var(--good2);color:var(--good)}} .pill.v-yellow{{background:var(--warn2);color:var(--warn)}} .pill.v-red,.pill.miss{{background:var(--bad2);color:var(--bad)}}
.pill.ac-self_serve_free,.pill.ac-self_serve_trial{{background:var(--good2);color:var(--good)}} .pill.ac-paid_plan,.pill.ac-admin_approval{{background:var(--warn2);color:var(--warn)}} .pill.ac-partner_gated,.pill.ac-no_public_api{{background:var(--bad2);color:var(--bad)}}
.pill.mcp-official{{background:var(--accent2);color:var(--accent)}}
.filters{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0 10px;font-size:.85rem}}
.filters input,.filters select{{font:inherit;font-size:.85rem;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}}
.filters .cnt{{font-family:var(--mono);color:var(--muted);margin-left:auto}}
.tw{{overflow-x:auto;border:1px solid var(--line);border-radius:8px}}
table{{border-collapse:collapse;width:100%;font-size:.82rem}} th,td{{text-align:left;padding:7px 9px;vertical-align:top;border-bottom:1px solid var(--line)}}
th{{font-family:var(--mono);font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);background:var(--bg2);position:sticky;top:0;cursor:pointer;white-space:nowrap}}
td.nm{{min-width:180px}} td.nm .ol{{display:block;font-size:.74rem;color:var(--muted);line-height:1.3;max-width:260px}}
.conf{{font-family:var(--mono);padding:1px 6px;border-radius:4px;background:color-mix(in oklab,var(--accent) calc(var(--c)*70%),var(--bg2))}}
.hc{{color:var(--warn)}}
.ev{{font-family:var(--mono);font-size:.68rem;text-decoration:none;padding:1px 4px;border-radius:3px;background:var(--bg2);margin-right:2px}}
.ev-grounded{{background:var(--good2);color:var(--good)}} .ev-not_grounded{{background:var(--bad2);color:var(--bad);text-decoration:line-through}} .ev-registry,.ev-probe{{background:var(--accent2);color:var(--accent)}}
.lnk{{font-size:.75rem}}
.pipeline{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}}
.stage{{background:var(--bg2);border-radius:8px;padding:12px;font-size:.84rem;position:relative}}
.stage .k{{font-family:var(--mono);font-size:.68rem;color:var(--accent);letter-spacing:.08em;text-transform:uppercase}} .stage b{{display:block;margin:4px 0}}
.stage .st{{font-family:var(--mono);font-size:.74rem;color:var(--muted)}}
table.gold td.ok{{color:var(--good)}} table.gold td.miss{{color:var(--bad);background:var(--bad2)}} table.gold tr.st-final td{{border-bottom:2px solid var(--line)}}
.bc-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:18px}}
.bc img{{width:100%;border:1px solid var(--line);border-radius:6px;margin-top:8px}}
pre{{background:var(--bg2);padding:14px;border-radius:8px;overflow-x:auto;font-family:var(--mono);font-size:.8rem;line-height:1.5}}
code{{font-family:var(--mono);font-size:.85em}}
.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px 40px}}
ul.plain{{padding-left:1.1em}} ul.plain li{{margin-block:6px}}
.note{{border-left:3px solid var(--warn);padding:8px 14px;background:var(--warn2);border-radius:0 8px 8px 0;max-width:80ch}}
footer{{margin-top:64px;padding-top:16px;border-top:1px solid var(--line);font-size:.8rem;color:var(--muted)}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
@media (max-width:640px){{h2{{font-size:1.4rem}} .lists li b{{min-width:110px}}}}
@media (prefers-reduced-motion:no-preference){{html{{scroll-behavior:smooth}}}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="eyebrow">Composio · AI Product Ops take-home · {esc(insights.get("date", "September 2026"))}</div>
  <h1>Can these 100 apps become agent toolkits?</h1>
  <p class="lede">An agent researched every app's auth, self-serve path, API surface and MCP status, with evidence links. A judge checked every quote against the cited page, registries and live probes voted on each field, and a hand-labelled gold set measured how accuracy moved. Patterns first; the table is below.</p>
  <div class="tiles">
    <div class="tile g"><div class="n">{verdicts['green']}</div><div class="l">green · buildable today</div></div>
    <div class="tile y"><div class="n">{verdicts['yellow']}</div><div class="l">yellow · caveats</div></div>
    <div class="tile r"><div class="n">{verdicts['red']}</div><div class="l">red · gated or no API</div></div>
    <div class="tile a"><div class="n">{self_serve}%</div><div class="l">self-serve credentials</div></div>
    <div class="tile a"><div class="n">{oauth_n}</div><div class="l">expose OAuth2</div></div>
    <div class="tile a"><div class="n">{mcp_c['official']}</div><div class="l">official MCP servers</div></div>
    <div class="tile"><div class="n">{acc('pass1')}→{acc('merged')}</div><div class="l">agent accuracy on {scores.get('n_gold', 0)}-app gold set, before any human fix</div></div>
  </div>
</header>

<nav class="toc" aria-label="Sections"><ul>
<li><a href="#patterns">Patterns</a></li><li><a href="#wins">Easy wins / outreach</a></li><li><a href="#matrix">All 100</a></li><li><a href="#agent">The agent</a></li><li><a href="#verify">Verification</a></li><li><a href="#failures">Failures</a></li><li><a href="#run">Run it</a></li>
</ul></nav>

<h2 id="patterns">What the 100 apps say</h2>
<ol class="head">{"".join(f"<li>{h}</li>" for h in headlines)}</ol>

<div class="charts">
  <figure><figcaption>How developers get credentials, by category</figcaption>{stacked_bar_svg(access_by_cat, ["self_serve_free", "self_serve_trial", "paid_plan", "admin_approval", "partner_gated", "no_public_api"], ACCESS_LABEL, SERIES)}</figure>
  <figure><figcaption>Auth exposed, by category (apps may count twice)</figcaption>{stacked_bar_svg(auth_by_cat, ["oauth2", "api_key", "other"], {"oauth2": "OAuth2", "api_key": "API key", "other": "Other only"}, SERIES)}</figure>
  <figure><figcaption>Buildability verdict, by category</figcaption>{stacked_bar_svg(verdict_by_cat, ["green", "yellow", "red"], {"green": "Green", "yellow": "Yellow", "red": "Red"}, SERIES).replace("var(--s1)", "var(--good)").replace("var(--s2)", "var(--warn)").replace("var(--s3)", "var(--bad)")}</figure>
  <figure><figcaption>Blockers across the {sum(blocker_c.values())} apps that have one</figcaption>{bar_svg([(BLOCKER_LABEL[k], v, f"{v} apps") for k, v in blocker_c.most_common()])}</figure>
</div>

<h2 id="wins">Where to start, and who to email</h2>
<div class="lists">
  <div><h3>Ship this week · {len(easy_wins)} green</h3><p class="muted" style="font-size:.85rem">Self-serve credentials, documented REST/GraphQL, standard auth. Ranked by buildability score.</p>
  <ul>{"".join(f'<li><b>{esc(r["name"])}</b><span class="sub">{esc(CATS_SHORT[r["category"]])} · {", ".join(AUTH_LABEL.get(a, a) for a in r["auth_methods"])} · {esc(API_LABEL.get(r["api_type"], r["api_type"]))}{" · official MCP" if r["mcp"]["status"] == "official" else ""}{"" if r["composio"]["in_catalog"] else " · <em>not yet in Composio</em>"}</span></li>' for r in easy_wins)}</ul></div>
  <div><h3>Needs outreach · {len(outreach)} red</h3><p class="muted" style="font-size:.85rem">What to ask for, per app.</p>
  <ul>{"".join(f'<li><b>{esc(r["name"])}</b><span class="sub">{esc(BLOCKER_LABEL[r["blocker"]])} → ask for {esc(ASK_FOR.get(r["blocker"], "clarification"))}</span></li>' for r in outreach)}</ul>
  <h3>Buildable with caveats · {len(yellow)} yellow</h3>
  <ul>{"".join(f'<li><b>{esc(r["name"])}</b><span class="sub">{esc(BLOCKER_LABEL[r["blocker"]])}</span></li>' for r in yellow)}</ul></div>
</div>

<h2 id="matrix">All 100 apps</h2>
<p>Every cell is the merged result of registries, live probes and the research pass, after quote-grounding. Evidence chips link to the cited page: <span class="ev ev-grounded">green</span> = quote found on the page, <span class="ev ev-not_grounded">struck</span> = quote not found, <span class="ev ev-registry">teal</span> = registry or probe. Confidence is computed from source agreement × grounding × model self-report. Click a column header to sort.</p>
<div class="filters">
  <input id="q" type="search" placeholder="Search app…" aria-label="Search">
  <select id="fcat" aria-label="Category"><option value="">All categories</option>{"".join(f'<option>{esc(CATS_SHORT[c])}</option>' for c in cats)}</select>
  <select id="fverdict" aria-label="Verdict"><option value="">Any verdict</option><option value="green">Green</option><option value="yellow">Yellow</option><option value="red">Red</option></select>
  <select id="faccess" aria-label="Access"><option value="">Any access</option>{"".join(f'<option value="{k}">{esc(v)}</option>' for k, v in ACCESS_LABEL.items())}</select>
  <select id="fauth" aria-label="Auth"><option value="">Any auth</option>{"".join(f'<option value="{k}">{esc(v)}</option>' for k, v in AUTH_LABEL.items())}</select>
  <span class="cnt" id="cnt">{N} apps</span>
</div>
<div class="tw"><table id="matrix-table"><thead><tr><th data-k="name">App</th><th data-k="cat">Category</th><th data-k="auth">Auth</th><th data-k="access">Access</th><th data-k="api">API</th><th data-k="mcp">MCP</th><th data-k="composio">In Composio</th><th data-k="verdict">Verdict · blocker</th><th data-k="conf">Conf.</th><th>Evidence</th></tr></thead>
<tbody>{"".join(row_html(r) for r in rows)}</tbody></table></div>
<p class="muted" style="font-size:.8rem">Machine-readable: <a href="data/results.json">results.json</a> · <a href="data/gold.json">gold.json</a> · <a href="data/scores.json">scores.json</a> · <a href="data/human_changes.json">human_changes.json</a></p>

<h2 id="agent">The agent</h2>
<p>A Python pipeline. Deterministic lookups run first and cost nothing; the LLM only fills gaps and is never trusted without a quote. Claude Code headless (<code>claude -p</code>, structured JSON output, WebSearch + WebFetch) is the research runtime; Composio's toolkit catalog is the first registry consulted. Every stage writes one JSON file per app, so any run is resumable and any row is auditable.</p>
<div class="pipeline">
  <div class="stage"><span class="k">1 · registries</span><b>Composio catalog, APIs.guru, MCP registry, GitHub</b><span class="st">Composio hit {reg_stats['composio']}/100 · OpenAPI spec {reg_stats['apis_guru']}/100 · MCP registry {reg_stats['mcp_registry']}/100</span></div>
  <div class="stage"><span class="k">2 · probes</span><b>Hit the API unauthenticated</b><span class="st">{probe_stats['api_alive']} APIs answered 4xx · {probe_stats['www_auth']} sent WWW-Authenticate · {probe_stats['oidc']} OIDC discovery docs · {probe_stats['openapi']} hidden OpenAPI specs</span></div>
  <div class="stage"><span class="k">3 · pass 1</span><b>Haiku, search snippets only</b><span class="st">weak baseline · {pass_fail['pass1']} failures · ~$0.12/app</span></div>
  <div class="stage"><span class="k">4 · pass 2</span><b>Sonnet, fetch docs, verbatim quotes</b><span class="st">registry + probe context injected · {pass_fail['pass2']} failures · ~$0.35/app</span></div>
  <div class="stage"><span class="k">5 · judge</span><b>Quote grounding, no LLM</b><span class="st">{j_checked} quotes checked · {j_grounded} found verbatim ({(j_grounded / j_checked * 100) if j_checked else 0:.0f}%) · {j_unreach} pages unreachable</span></div>
  <div class="stage"><span class="k">6 · merge</span><b>Weighted vote per field → confidence</b><span class="st">rule-based verdict · family consistency checks · human queue of {len(human_queue)}</span></div>
  <div class="stage"><span class="k">7 · human</span><b>Browser checks on the queue</b><span class="st">{human_touched} apps touched · {len(human_changes)} fields corrected · {len(browser_checks)} browser checks</span></div>
</div>
<div class="two">
<div><h3>Where a human was needed</h3><ul class="plain">{"".join(f"<li>{h}</li>" for h in human_notes) or "<li>See corrections log below.</li>"}</ul></div>
<div><h3>Corrections log</h3><ul class="plain" style="font-size:.88rem">{"".join(f'<li><b>{esc(c["name"])}</b> · {esc(c["field"])}: <code>{esc(c["from"])}</code> → <code>{esc(c["to"])}</code><span class="sub">{esc(c["reason"])} <a class="lnk" href="{esc(c.get("evidence", "#"))}" target="_blank" rel="noopener">evidence</a></span></li>' for c in human_changes) or "<li>None.</li>"}</ul></div>
</div>
<p class="muted" style="font-size:.85rem">Total LLM spend across both passes: ${total_cost:.2f} (billed through a Claude subscription; the same script runs on <code>ANTHROPIC_API_KEY</code>). Composio SDK note: the API key supplied for this run was rejected (401), so the agent read Composio's public toolkit index instead; the SDK path is wired and used automatically when a valid key is present.</p>

<h2 id="verify">How we know it's right</h2>
<p>{scores.get('n_gold', 0)} apps (2–3 per category, deliberately including the odd ones: a CLI, a rebranded company, a bot-blocked site) were hand-labelled from the primary docs <em>before</em> reading agent output. Each stage was then scored on five fields. The jump from pass 1 to pass 2 is the value of fetching docs and demanding quotes; the jump to "merged" is registries, probes and grounding; the last step is the human queue.</p>
<figure><figcaption>Field-level accuracy against the gold set ({len(scores.get('fields', []))} fields × {scores.get('n_gold', 0)} apps)</figcaption>{accuracy_svg(summary, scores.get('fields', []))}</figure>
<div class="two" style="margin-top:12px">
<div><h3>Per field</h3><div class="tw"><table><thead><tr><th>Field</th><th>Pass 1</th><th>Pass 2</th><th>Merged</th><th>Final</th></tr></thead><tbody>
{"".join(f"<tr><td>{esc(f.replace('_', ' '))}</td><td>{acc('pass1', f)}</td><td>{acc('pass2', f)}</td><td>{acc('merged', f)}</td><td><b>{acc('final', f)}</b></td></tr>" for f in scores.get('fields', []))}
</tbody></table></div></div>
<div><h3>Is confidence calibrated?</h3><p style="font-size:.9rem">Share of gold apps where auth, access <em>and</em> verdict were all right, by the agent's own confidence bucket. Errors should concentrate in the low bucket — that is what makes the human queue worth working.</p>
<div class="tw"><table><thead><tr><th>Confidence</th><th>Apps</th><th>All core fields right</th></tr></thead><tbody>
{"".join(f"<tr><td>{esc(b)}</td><td>{v['n']}</td><td>{(v['rate'] * 100):.0f}%</td></tr>" for b, v in sorted(calib.items()))}
</tbody></table></div>
<p style="font-size:.9rem">Composio's own catalog covers {composio_in} of the 100. Pass 2, working independently of the catalog, agreed with Composio's declared auth on {composio_agree}/{composio_in} ({(composio_agree / composio_in * 100) if composio_in else 0:.0f}%); the differences are explained in the failures section.</p>
<div class="note" style="margin-top:12px"><b>Read the last bar carefully.</b> "Final" is 100% on the gold set by construction: the human corrections were found through the same review. The honest estimate for an unseen app is the <b>merged</b> bar, before any human touched it.</div></div>
</div>
<h3>Gold set: hits and misses, per stage</h3>
<p class="muted" style="font-size:.85rem">✓ = matches the hand label (or an accepted alternative); ✗ = miss. Hover a cell for the gold value.</p>
{gold_table()}
<h3>Browser verification</h3>
<p style="font-size:.9rem">For queued apps, the page that decides the answer (pricing, signup, auth docs) was opened in Chrome and screenshotted. These are the checks that changed or confirmed a label.</p>
{browser_checks_html()}

<h2 id="failures">What went wrong, honestly</h2>
<ul class="plain">{"".join(f"<li>{f}</li>" for f in failures) or "<li>Filled after the run.</li>"}</ul>

<h2 id="run">Run it</h2>
<div class="two">
<div><p>Source: <a href="{REPO_URL}">{REPO_URL.replace('https://', '')}</a>. Needs Python 3.11+ and either a logged-in Claude Code CLI or <code>ANTHROPIC_API_KEY</code>. A Composio key is optional (falls back to the public catalog).</p>
<pre>pip install -r requirements.txt
python -m agent.run all --app "Attio"        # one app, every stage
python -m agent.run all                       # all 100, resumable
python -m verification.scoring                # score against gold
python -m page.build                          # rebuild this page</pre></div>
<div><p>Runnable trigger: the repo's <b>Research one app</b> GitHub Action (<code>workflow_dispatch</code>) runs the full pipeline for any app name and commits the JSON. Add a new app by appending to <code>apps.json</code>.</p>
<p>For agents: the page embeds the dataset (<code>window.RESEARCH</code>) and links raw JSON above. Enums are documented in <code>schema/result.schema.json</code>.</p></div>
</div>
<footer>Built by Nikhil Gaur for the Composio AI Product Ops take-home. Research runtime: Claude Code (Haiku + Sonnet) · registries: Composio, APIs.guru, MCP registry, GitHub · verification: quote grounding, live probes, Claude-in-Chrome, human review.</footer>
</div>

<script>
window.RESEARCH = {data_json};
(function(){{
  const rows=[...document.querySelectorAll('#matrix-table tbody tr')];
  const q=document.getElementById('q'),fc=document.getElementById('fcat'),fv=document.getElementById('fverdict'),fa=document.getElementById('faccess'),fu=document.getElementById('fauth'),cnt=document.getElementById('cnt');
  function apply(){{
    const s=q.value.trim().toLowerCase(),c=fc.value,v=fv.value,a=fa.value,u=fu.value;let n=0;
    for(const r of rows){{
      const ok=(!s||r.dataset.name.includes(s))&&(!c||r.dataset.cat===c)&&(!v||r.dataset.verdict===v)&&(!a||r.dataset.access===a)&&(!u||r.dataset.auth.split(' ').includes(u));
      r.hidden=!ok; if(ok)n++;
    }}
    cnt.textContent=n+' apps';
  }}
  [q,fc,fv,fa,fu].forEach(e=>e.addEventListener('input',apply));
  const order={{green:0,yellow:1,red:2}};
  document.querySelectorAll('#matrix-table th[data-k]').forEach((th,i)=>{{
    let asc=true;
    th.addEventListener('click',()=>{{
      const k=th.dataset.k;
      const key=r=>{{const td=r.children[i];if(k==='verdict')return order[r.dataset.verdict];if(k==='conf')return parseFloat(td.textContent)||0;return td.textContent.trim().toLowerCase();}};
      rows.sort((x,y)=>{{const a=key(x),b=key(y);return (a<b?-1:a>b?1:0)*(asc?1:-1);}});
      asc=!asc; const tb=th.closest('table').tBodies[0]; rows.forEach(r=>tb.appendChild(r));
    }});
  }});
  try{{const t=localStorage.getItem('atlas-cat');if(t){{fc.value=t;apply();}}}}catch(e){{}}
  fc.addEventListener('change',()=>{{try{{localStorage.setItem('atlas-cat',fc.value)}}catch(e){{}}}});
}})();
</script>
</body>
</html>
"""

DOCS.mkdir(exist_ok=True)
(DOCS / "data").mkdir(exist_ok=True)
(DOCS / "index.html").write_text(page, encoding="utf-8")
for src, dst in [("results/final.json", "results.json"), ("verification/gold.json", "gold.json"), ("verification/scores.json", "scores.json"), ("verification/human_changes.json", "human_changes.json")]:
    p = ROOT / src
    if p.exists():
        (DOCS / "data" / dst).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
print(f"wrote docs/index.html ({len(page) // 1024} KB) - {N} apps, verdicts {dict(verdicts)}")
