#!/usr/bin/env python3
"""One-off read-only diagnostic: fetch the sitemap and print every URL whose
classify_url() theme is "unknown", grouped by the slug's first path segment,
so THEME_PREFIXES can be extended to cover them. Not part of the audit
pipeline; delete once theme coverage is fixed.
"""
from collections import defaultdict

from run_audit import classify_url, fetch_sitemap_urls

urls = fetch_sitemap_urls()
print(f"total sitemap URLs: {len(urls)}")

by_prefix = defaultdict(list)
for u in urls:
    c = classify_url(u)
    if c["theme"] == "unknown":
        slug = c["slug"] or u
        prefix = slug.split("-")[0] if slug else "(empty)"
        by_prefix[prefix].append(u)

total_unknown = sum(len(v) for v in by_prefix.values())
print(f"unknown-theme URLs: {total_unknown}")
for prefix, group in sorted(by_prefix.items(), key=lambda kv: -len(kv[1])):
    print(f"\n-- prefix '{prefix}' ({len(group)}) --")
    for u in group[:5]:
        print(f"   {u}")
    if len(group) > 5:
        print(f"   ... and {len(group) - 5} more")
