#!/usr/bin/env python3
"""One-off read-only diagnostic: fetch one page's current raw HTML and dump
everything relevant to its JSON-LD Dataset output, plus signals that might
point at which WordPress template/plugin generated it (generator meta tag,
enqueued script/style handles, body/post classes, html comments). GET only,
reuses run_audit.py's fetch_with_retry. Not part of the audit pipeline;
delete once the investigation is done.
"""
import json
import re

from bs4 import BeautifulSoup

from run_audit import fetch_with_retry

URL = "https://app-navi.biz/station-passenger-ranking-national/"

resp, err = fetch_with_retry(URL)
if err or resp is None or resp.status_code != 200:
    print(f"FETCH FAILED: {err or resp.status_code}")
    raise SystemExit(1)

html = resp.text
print(f"status: {resp.status_code}  bytes: {len(html)}")

soup = BeautifulSoup(html, "html.parser")

generator = soup.find("meta", attrs={"name": "generator"})
print(f"\ngenerator meta: {generator}")

body = soup.find("body")
print(f"body class: {body.get('class') if body else None}")

article = soup.find("article")
print(f"article id/class: {(article.get('id'), article.get('class')) if article else None}")

ldjson_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
print(f"\nnumber of <script type=application/ld+json> tags on page: {len(ldjson_scripts)}")

dataset_entries = []
for i, script in enumerate(ldjson_scripts, start=1):
    raw = script.string or ""
    print(f"\n----- ld+json block {i} (raw, {len(raw)} chars) -----")
    print(raw[:3000])
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"[parse error: {exc}]")
        continue
    candidates = data.get("@graph", [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    for cand in candidates:
        if isinstance(cand, dict) and cand.get("@type") in ("Dataset", ["Dataset"]):
            dataset_entries.append({
                "block_index": i,
                "name": cand.get("name"),
                "description": cand.get("description"),
                "license": cand.get("license"),
                "url": cand.get("url"),
                "identifier": cand.get("identifier"),
                "same_as": cand.get("sameAs"),
            })

print(f"\n\n===== Dataset entries found: {len(dataset_entries)} =====")
for d in dataset_entries:
    print(json.dumps(d, ensure_ascii=False, indent=2))

# duplicate check: same name+identifier appearing more than once
seen = {}
for d in dataset_entries:
    key = (d.get("name"), d.get("identifier"), d.get("url"))
    seen[key] = seen.get(key, 0) + 1
dupes = {k: v for k, v in seen.items() if v > 1}
print(f"\nduplicate Dataset entries (same name/identifier/url repeated): {dupes}")

# look for likely plugin/theme fingerprints in raw HTML (shortcode wrapper divs,
# enqueued handles containing "vwd", "library", "dataset", "schema", "jsonld")
print("\n----- possible source fingerprints in raw HTML -----")
for pattern in [r'class="[^"]*vwd-library[^"]*"', r'id="[^"]*jsonld[^"]*"', r'id="[^"]*schema[^"]*"',
                 r"<!--\s*(vwd|library|dataset|jsonld|schema)[^>]*-->", r'wp-content/plugins/[a-zA-Z0-9_-]+']:
    matches = sorted(set(re.findall(pattern, html, flags=re.IGNORECASE)))
    if matches:
        print(f"{pattern}: {matches[:10]}")
