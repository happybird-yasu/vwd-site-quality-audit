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
from collections import defaultdict
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

SITE = "https://app-navi.biz"
SEARCH_API = f"{SITE}/wp-json/vwd-library/v1/search"
USER_AGENT = "VWD-Site-Quality-Audit/1.0 (+internal content QA pilot; read-only; contact: hirohirori)"
REQUEST_TIMEOUT = 15
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 2
REQUEST_DELAY_SECONDS = 1.5
CATALOG_PAGE_CAP = 10
RANDOM_SEED = 20261006

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


def fetch_catalog():
    items = []
    page = 1
    total_pages = None
    while page <= CATALOG_PAGE_CAP and (total_pages is None or page <= total_pages):
        resp, err = fetch_with_retry(SEARCH_API, params={"per_page": 100, "page": page})
        if err or resp is None or resp.status_code != 200:
            print(f"[catalog] page {page} failed: {err or resp.status_code}", file=sys.stderr)
            break
        data = resp.json()
        items.extend(data.get("items", []))
        total_pages = data.get("total_pages", page)
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)
    return items


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

    themes = sorted(by_theme.keys())
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

        soup = BeautifulSoup(resp.text, "html.parser")
        text = extract_main_text(soup)
        internal, external, external_samples = classify_links(soup, url)
        datasets = find_dataset_jsonld(soup)

        pages.append({
            "id": item["id"],
            "theme": item.get("theme"),
            "region": item.get("region"),
            "language": item.get("language"),
            "edition": item.get("edition"),
            "title": item.get("title"),
            "url": url,
            "status_code": resp.status_code,
            "char_count": len(text),
            "internal_links": internal,
            "external_links": external,
            "external_link_samples": external_samples,
            "dataset_jsonld_found": len(datasets) > 0,
            "dataset_entries": datasets,
            "_text": text,
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


def summarize(pages, failures, catalog_size):
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
        "catalog_total_items": catalog_size,
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
    lines.append(f"# VWD Site Quality Audit — Pilot Run\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append(f"- Sample size: {summary['sample_size']} (fetched OK: {summary['fetched_ok']}, failed: {summary['failed']})")
    lines.append(f"- Catalog total items (public search API): {summary['catalog_total_items']}")
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
    lines.append("| URL | Theme | Region | Lang | Chars | Internal links | External links | Sim. to national | Max sim. in theme | Est. unique ratio | Dataset |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for p in clean_pages:
        lines.append(
            f"| {p['url']} | {p['theme']} | {p['region']} | {p['language']} | {p['char_count']} | "
            f"{p['internal_links']} | {p['external_links']} | {p['similarity_to_national']} | "
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


def write_load_estimate(pages, catalog_size, out_dir, elapsed_seconds):
    per_page = elapsed_seconds / max(len(pages), 1)
    full_estimate_seconds = per_page * catalog_size
    lines = [
        "# Estimated load for a full-site crawl\n",
        f"- Pilot sample: {len(pages)} pages fetched in {round(elapsed_seconds, 1)}s "
        f"({round(per_page, 2)}s/page, includes {REQUEST_DELAY_SECONDS}s politeness delay)",
        f"- Catalog total (public search API): {catalog_size} items",
        f"- Extrapolated full crawl: ~{round(full_estimate_seconds / 60, 1)} minutes, "
        f"{catalog_size} GET requests to app-navi.biz, single-threaded, "
        f"{REQUEST_DELAY_SECONDS}s delay between requests",
        "- All requests are read-only GETs to public article URLs discovered via the public "
        "`vwd-library/v1/search` API; no WordPress admin, Xserver panel, or write endpoints are touched.",
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

    print("[2/5] fetching public catalog via vwd-library/v1/search")
    catalog = fetch_catalog()
    print(f"      catalog size: {len(catalog)}")
    if not catalog:
        print("ERROR: empty catalog, aborting (site may be unreachable)", file=sys.stderr)
        sys.exit(1)

    print(f"[3/5] selecting representative sample (~{args.sample_size} pages)")
    sample = build_sample(catalog, args.sample_size)
    print(f"      selected: {len(sample)} pages across {len({s['theme'] for s in sample})} themes")

    print("[4/5] crawling sample pages (read-only GET, max 2 attempts each)")
    t0 = time.time()
    pages, failures = crawl_pages(sample, disallow_prefixes)
    elapsed = time.time() - t0
    pages = compute_similarity(pages)

    print("[5/5] writing report")
    summary = summarize(pages, failures, len(catalog))
    write_report(pages, failures, summary, {"selected": sample, "disallow_prefixes": list(disallow_prefixes)}, out_dir)
    write_load_estimate(pages, len(catalog), out_dir, elapsed)

    print(f"\nDone. Report written to {out_dir}/report.md")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
