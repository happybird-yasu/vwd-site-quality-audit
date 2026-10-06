#!/usr/bin/env python3
import argparse
import difflib
import json
import os
import random
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

SITE = "https://app-navi.biz"
SITEMAP_URL = f"{SITE}/post-sitemap.xml"
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
SITEMAP_RECURSION_CAP = 5
USER_AGENT = "VWD-Site-Quality-Audit/1.0 (+internal content QA pilot; read-only; contact: hirohirori)"
REQUEST_TIMEOUT = 15
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 2
REQUEST_DELAY_SECONDS = 1.5
RANDOM_SEED = 20261006

LANG_CODES = {"en", "es", "de", "fr"}
THEME_PREFIXES = [
    ("population", "population"),
    ("station-map", "station"),
    ("road-traffic", "road"),
    ("airport-passengers", "airport"),
    ("port-passenger", "port"),
    ("domestic-travel", "travel"),
    ("hotel-ryokan", "hotel_ryokan"),
    ("infection-weekly", "infection"),
    ("onsen-map", "onsen"),
    ("mineral-map", "minerals"),
    ("school-count-map", "schools"),
    ("medical-count-map", "medical"),
]
# ordered longest-composite-first so e.g. "chubu-hokuriku" matches before "chubu"
REGION_KEYWORDS = [
    "chubu-hokuriku", "chugoku-kyushu", "kyushu-okinawa",
    "hokkaido", "tohoku", "kanto", "koshinetsu", "chubu",
    "hokuriku", "kinki", "chugoku", "shikoku", "kyushu", "okinawa",
]

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def fetch_with_retry(url, params=None):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            return resp, None
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS)
    return None, last_error


def fetch_robots_disallow():
    resp, err = fetch_with_retry(f"{SITE}/robots.txt")
    if err or resp is None or resp.status_code != 200:
        return set()
    disallow = set()
    for line in resp.text.splitlines():
        line = line.strip()
        if line.lower().startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path:
                disallow.add(path)
    return disallow


def is_disallowed(url, disallow_prefixes):
    path = urllib.parse.urlparse(url).path
    return any(path.startswith(p) for p in disallow_prefixes)


def parse_sitemap_xml(xml_bytes):
    """Returns (kind, urls) where kind is 'index' or 'urlset'."""
    root = ET.fromstring(xml_bytes)
    tag = root.tag.replace(SITEMAP_NS, "")
    locs = [el.text.strip() for el in root.iter(f"{SITEMAP_NS}loc") if el.text and el.text.strip()]
    return tag, locs


def fetch_sitemap_urls(start_url=SITEMAP_URL, depth=0, seen=None):
    if seen is None:
        seen = set()
    if depth > SITEMAP_RECURSION_CAP or start_url in seen:
        return []
    seen.add(start_url)

    resp, err = fetch_with_retry(start_url)
    time.sleep(REQUEST_DELAY_SECONDS)
    if err or resp is None or resp.status_code != 200:
        print(f"[sitemap] {start_url} failed: {err or resp.status_code}", file=sys.stderr)
        return []

    try:
        kind, locs = parse_sitemap_xml(resp.content)
    except ET.ParseError as exc:
        print(f"[sitemap] {start_url} parse error: {exc}", file=sys.stderr)
        return []

    if kind == "sitemapindex":
        child_sitemaps = [loc for loc in locs if "post-sitemap" in loc] or locs
        urls = []
        for child in child_sitemaps:
            urls.extend(fetch_sitemap_urls(child, depth + 1, seen))
        return urls
    return locs


def classify_url(url):
    parsed = urllib.parse.urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    slug = segments[-1] if segments else ""

    language = "ja"
    if segments and segments[0] in LANG_CODES:
        language = segments[0]
    lang_suffix_match = re.search(r"-(en|es|de|fr)$", slug)
    if lang_suffix_match:
        language = lang_suffix_match.group(1)

    core = re.sub(r"-(ja|en|es|de|fr)$", "", slug)

    theme = "unknown"
    for prefix, key in THEME_PREFIXES:
        if core == prefix or core.startswith(prefix + "-"):
            theme = key
            break

    if theme == "infection" or core in ("port-passenger-map",) or "national" in core:
        region = "national"
    else:
        region = "other"
        for kw in REGION_KEYWORDS:
            if kw in core:
                region = kw
                break

    return {"slug": slug, "theme": theme, "region": region, "language": language, "url": url, "id": url}


def build_sample(items, sample_size):
    by_theme = defaultdict(list)
    for it in items:
        by_theme[it.get("theme", "unknown")].append(it)

    rng = random.Random(RANDOM_SEED)
    selected = []
    seen_ids = set()

    def take(candidates, n):
        pool = [c for c in candidates if c["id"] not in seen_ids]
        rng.shuffle(pool)
        picked = pool[:n]
        for p in picked:
            seen_ids.add(p["id"])
        return picked

    themes = sorted(k for k in by_theme.keys() if k != "unknown")
    for theme in themes:
        group = by_theme[theme]
        national = [g for g in group if g.get("region") == "national" and g.get("language") == "ja"]
        regional_ja = [g for g in group if g.get("region") != "national" and g.get("language") == "ja"]
        other_lang = [g for g in group if g.get("language") != "ja"]

        selected += take(national, 1)
        selected += take(regional_ja, 2)
        selected += take(other_lang, 1)

        if len(selected) >= sample_size:
            break

    if len(selected) < sample_size:
        remaining = [it for it in items if it["id"] not in seen_ids]
        rng.shuffle(remaining)
        for it in remaining:
            if len(selected) >= sample_size:
                break
            selected.append(it)
            seen_ids.add(it["id"])

    return selected[:sample_size]


def extract_main_text(soup):
    for sel in ["script", "style", "nav", "header", "footer"]:
        for tag in soup.select(sel):
            tag.decompose()
    for cls in ["vwd-article-footer", "mm-wrap", "vwd-library"]:
        for tag in soup.select(f".{cls}"):
            tag.decompose()
    container = soup.select_one("article") or soup.select_one("main") or soup.body
    if container is None:
        return ""
    text = container.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def classify_links(soup, page_url):
    host = urllib.parse.urlparse(page_url).netloc
    internal, external = 0, 0
    external_samples = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        parsed = urllib.parse.urlparse(urllib.parse.urljoin(page_url, href))
        if not parsed.netloc or parsed.netloc == host:
            internal += 1
        else:
            external += 1
            if len(external_samples) < 5:
                external_samples.append(parsed.geturl())
    return internal, external, external_samples


def find_dataset_jsonld(soup):
    found = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data.get("@graph", [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for cand in candidates:
            if isinstance(cand, dict) and cand.get("@type") in ("Dataset", ["Dataset"]):
                found.append({
                    "has_description": bool(cand.get("description")),
                    "has_license": bool(cand.get("license")),
                    "has_name": bool(cand.get("name")),
                })
    return found


def parse_page(html, url):
    """Pure, network-free parse of one page's HTML. Order matters: title,
    Dataset JSON-LD, and whole-page link counts must all be read BEFORE
    extract_main_text() mutates the tree (it decompose()s
    <script>/<nav>/<header>/<footer>, which would otherwise silently delete
    the JSON-LD <script> tags first)."""
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else None
    datasets = find_dataset_jsonld(soup)
    page_internal, page_external, page_external_samples = classify_links(soup, url)

    text = extract_main_text(soup)
    body_internal, body_external, body_external_samples = classify_links(soup, url)

    return {
        "title": title,
        "char_count": len(text),
        "page_internal_links": page_internal,
        "page_external_links": page_external,
        "page_external_link_samples": page_external_samples,
        "body_internal_links": body_internal,
        "body_external_links": body_external,
        "body_external_link_samples": body_external_samples,
        "dataset_jsonld_found": len(datasets) > 0,
        "dataset_entries": datasets,
        "_text": text,
    }


def crawl_pages(sample, disallow_prefixes):
    pages = []
    failures = []
    for item in sample:
        url = item["url"]
        if is_disallowed(url, disallow_prefixes):
            failures.append({"url": url, "reason": "disallowed_by_robots_txt"})
            continue

        resp, err = fetch_with_retry(url)
        time.sleep(REQUEST_DELAY_SECONDS)

        if err or resp is None:
            failures.append({"url": url, "reason": err or "no_response"})
            continue
        if resp.status_code != 200:
            failures.append({"url": url, "reason": f"http_{resp.status_code}"})
            continue

        parsed = parse_page(resp.text, url)

        pages.append({
            "id": item["id"],
            "theme": item.get("theme"),
            "region": item.get("region"),
            "language": item.get("language"),
            "slug": item.get("slug"),
            "url": url,
            "status_code": resp.status_code,
            **parsed,
        })
    return pages, failures


def compute_similarity(pages):
    by_theme = defaultdict(list)
    for p in pages:
        by_theme[p["theme"]].append(p)

    for p in pages:
        siblings = [s for s in by_theme[p["theme"]] if s["id"] != p["id"]]
        best_ratio = 0.0
        best_sibling = None
        unique_chars = len(p["_text"])
        if siblings and p["_text"]:
            for s in siblings:
                if not s["_text"]:
                    continue
                sm = difflib.SequenceMatcher(None, p["_text"], s["_text"], autojunk=False)
                ratio = sm.ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_sibling = s["id"]
                    matched = sum(block.size for block in sm.get_matching_blocks())
                    unique_chars = max(0, len(p["_text"]) - matched)
        p["theme_peer_count"] = len(siblings)
        p["max_similarity_in_theme"] = round(best_ratio, 4)
        p["most_similar_peer_id"] = best_sibling
        p["estimated_unique_chars"] = unique_chars
        p["estimated_unique_ratio"] = round(unique_chars / len(p["_text"]), 4) if p["_text"] else 0.0

        national = next((s for s in by_theme[p["theme"]] if s.get("region") == "national" and s["id"] != p["id"]), None)
        if national and p.get("region") != "national" and p["_text"] and national["_text"]:
            sm = difflib.SequenceMatcher(None, p["_text"], national["_text"], autojunk=False)
            p["similarity_to_national"] = round(sm.ratio(), 4)
        else:
            p["similarity_to_national"] = None

    return pages


def summarize(pages, failures, sitemap_url_count):
    themes = defaultdict(list)
    for p in pages:
        themes[p["theme"]].append(p)

    flagged = [
        p for p in pages
        if p["max_similarity_in_theme"] >= 0.6 or p["estimated_unique_ratio"] < 0.3
    ]

    avg_chars = round(sum(p["char_count"] for p in pages) / len(pages), 1) if pages else 0
    dataset_coverage = sum(1 for p in pages if p["dataset_jsonld_found"])
    dataset_missing_desc = [
        p["url"] for p in pages
        if p["dataset_jsonld_found"] and not any(d["has_description"] for d in p["dataset_entries"])
    ]
    dataset_absent = [p["url"] for p in pages if not p["dataset_jsonld_found"]]

    return {
        "sample_size": len(pages) + len(failures),
        "fetched_ok": len(pages),
        "failed": len(failures),
        "sitemap_url_count": sitemap_url_count,
        "avg_char_count": avg_chars,
        "dataset_jsonld_present_count": dataset_coverage,
        "dataset_jsonld_missing_description_urls": dataset_missing_desc,
        "dataset_jsonld_absent_urls": dataset_absent,
        "flagged_high_similarity_or_low_unique": [
            {"url": p["url"], "theme": p["theme"], "max_similarity_in_theme": p["max_similarity_in_theme"],
             "estimated_unique_ratio": p["estimated_unique_ratio"]}
            for p in flagged
        ],
        "themes_covered": sorted(themes.keys()),
    }


def write_report(pages, failures, summary, sample_meta, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    clean_pages = [{k: v for k, v in p.items() if k != "_text"} for p in pages]

    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "pages": clean_pages}, f, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "sample.json"), "w", encoding="utf-8") as f:
        json.dump(sample_meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "failures.jsonl"), "w", encoding="utf-8") as f:
        for item in failures:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    lines = []
    lines.append("# VWD Site Quality Audit — Pilot Run\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append("Population source: `post-sitemap.xml` (REST API is disabled site-side; not used here).\n")
    lines.append(f"- Sample size: {summary['sample_size']} (fetched OK: {summary['fetched_ok']}, failed: {summary['failed']})")
    lines.append(f"- Sitemap total URLs discovered: {summary['sitemap_url_count']}")
    lines.append(f"- Average body char count (sampled pages): {summary['avg_char_count']}")
    lines.append(f"- Dataset JSON-LD present: {summary['dataset_jsonld_present_count']} / {summary['fetched_ok']}")
    lines.append(f"- Themes covered: {', '.join(summary['themes_covered'])}\n")

    lines.append("## Dataset structured data issues\n")
    if summary["dataset_jsonld_missing_description_urls"]:
        lines.append("Dataset present but missing `description`:\n")
        for u in summary["dataset_jsonld_missing_description_urls"]:
            lines.append(f"- {u}")
    if summary["dataset_jsonld_absent_urls"]:
        lines.append("\nNo Dataset JSON-LD found at all:\n")
        for u in summary["dataset_jsonld_absent_urls"]:
            lines.append(f"- {u}")
    lines.append("")

    lines.append("## Flagged pages (high template similarity or low unique-text ratio)\n")
    if summary["flagged_high_similarity_or_low_unique"]:
        lines.append("| URL | Theme | Max similarity in theme | Est. unique ratio |")
        lines.append("|---|---|---|---|")
        for f in summary["flagged_high_similarity_or_low_unique"]:
            lines.append(f"| {f['url']} | {f['theme']} | {f['max_similarity_in_theme']} | {f['estimated_unique_ratio']} |")
    else:
        lines.append("None in this sample.")
    lines.append("")

    lines.append("## Full sample detail\n")
    lines.append(
        "| URL | Theme | Region | Lang | Chars | Body internal links | Body external links | "
        "Page internal links | Page external links | Sim. to national | Max sim. in theme | Est. unique ratio | Dataset |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for p in clean_pages:
        lines.append(
            f"| {p['url']} | {p['theme']} | {p['region']} | {p['language']} | {p['char_count']} | "
            f"{p['body_internal_links']} | {p['body_external_links']} | "
            f"{p['page_internal_links']} | {p['page_external_links']} | {p['similarity_to_national']} | "
            f"{p['max_similarity_in_theme']} | {p['estimated_unique_ratio']} | {'yes' if p['dataset_jsonld_found'] else 'no'} |"
        )
    lines.append("")

    if failures:
        lines.append("## Failures\n")
        lines.append("| URL | Reason |")
        lines.append("|---|---|")
        for fl in failures:
            lines.append(f"| {fl['url']} | {fl['reason']} |")
        lines.append("")

    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_load_estimate(pages, sitemap_url_count, out_dir, elapsed_seconds):
    per_page = elapsed_seconds / max(len(pages), 1)
    full_estimate_seconds = per_page * sitemap_url_count
    lines = [
        "# Estimated load for a full-site crawl\n",
        f"- Pilot sample: {len(pages)} pages fetched in {round(elapsed_seconds, 1)}s "
        f"({round(per_page, 2)}s/page, includes {REQUEST_DELAY_SECONDS}s politeness delay)",
        f"- Sitemap total URLs discovered: {sitemap_url_count}",
        f"- Extrapolated full crawl: ~{round(full_estimate_seconds / 60, 1)} minutes, "
        f"{sitemap_url_count} GET requests to app-navi.biz, single-threaded, "
        f"{REQUEST_DELAY_SECONDS}s delay between requests",
        "- All requests are read-only GETs to public article URLs discovered via `post-sitemap.xml`; "
        "the REST API is not used (it is intentionally disabled site-side). No WordPress admin, "
        "Xserver panel, or write endpoints are touched.",
    ]
    with open(os.path.join(out_dir, "estimated_load.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=int(os.environ.get("SAMPLE_SIZE", 26)))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out or os.path.join("results", f"run-{run_id}")

    print("[1/5] fetching robots.txt")
    disallow_prefixes = fetch_robots_disallow()

    print("[2/5] fetching public post sitemap")
    sitemap_urls = fetch_sitemap_urls()
    print(f"      sitemap URLs: {len(sitemap_urls)}")
    if not sitemap_urls:
        print("ERROR: empty sitemap, aborting (site may be unreachable, or sitemap path changed)", file=sys.stderr)
        sys.exit(1)

    classified = [classify_url(u) for u in sitemap_urls]
    unknown_theme = sum(1 for c in classified if c["theme"] == "unknown")
    if unknown_theme:
        print(f"      note: {unknown_theme} URLs did not match a known theme prefix (left out of theme-stratified picks)")

    print(f"[3/5] selecting representative sample (~{args.sample_size} pages)")
    sample = build_sample(classified, args.sample_size)
    print(f"      selected: {len(sample)} pages across {len({s['theme'] for s in sample})} themes")

    print("[4/5] crawling sample pages (read-only GET, max 2 attempts each)")
    t0 = time.time()
    pages, failures = crawl_pages(sample, disallow_prefixes)
    elapsed = time.time() - t0
    pages = compute_similarity(pages)

    print("[5/5] writing report")
    summary = summarize(pages, failures, len(sitemap_urls))
    write_report(pages, failures, summary, {"selected": sample, "disallow_prefixes": list(disallow_prefixes)}, out_dir)
    write_load_estimate(pages, len(sitemap_urls), out_dir, elapsed)

    print(f"\nDone. Report written to {out_dir}/report.md")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
