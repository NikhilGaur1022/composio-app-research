"""Shared helpers: paths, slugs, disk cache, HTTP fetch."""
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

APPS_FILE = ROOT / "apps.json"
SCHEMA_FILE = ROOT / "schema" / "result.schema.json"
CACHE_DIR = ROOT / "cache"
RESULTS_DIR = ROOT / "results"
CACHE_DIR.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (compatible; composio-research-agent/0.1; +https://github.com)"


def load_apps():
    return json.loads(APPS_FILE.read_text(encoding="utf-8"))


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s


def cache_path(kind: str, key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    d = CACHE_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"


def cached(kind: str, key: str, fn, ttl_days: float = 30):
    p = cache_path(kind, key)
    if p.exists() and (time.time() - p.stat().st_mtime) < ttl_days * 86400:
        return json.loads(p.read_text(encoding="utf-8"))
    val = fn()
    p.write_text(json.dumps(val, ensure_ascii=False), encoding="utf-8")
    return val


def fetch(url: str, timeout: float = 20, allow_redirects=True, method="GET", headers=None) -> dict:
    """HTTP fetch with caching. Returns {status, final_url, headers, text, error}."""
    def _do():
        try:
            with httpx.Client(follow_redirects=allow_redirects, timeout=timeout,
                              headers={"User-Agent": UA, **(headers or {})}) as c:
                r = c.request(method, url)
                text = r.text if "text" in r.headers.get("content-type", "") or "json" in r.headers.get("content-type", "") or "xml" in r.headers.get("content-type", "") else ""
                return {"status": r.status_code, "final_url": str(r.url),
                        "headers": {k.lower(): v for k, v in r.headers.items()},
                        "text": text if "json" in r.headers.get("content-type", "") else text[:400_000], "error": None}
        except Exception as e:  # noqa: BLE001
            return {"status": 0, "final_url": url, "headers": {}, "text": "", "error": f"{type(e).__name__}: {e}"[:300]}
    return cached("http", f"{method} {url}", _do)


_TAG = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def html_to_text(html: str) -> str:
    t = _TAG.sub(" ", html)
    t = _TAGS.sub(" ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return _WS.sub(" ", t).strip()


def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return _WS.sub(" ", s).strip()


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
