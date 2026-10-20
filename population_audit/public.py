"""Public-side data of https://app-navi.biz/maps/population/ (read-only GET).

Each regional page embeds POP_DATA (total flows + top10) per year/direction.
Attribute JSON files (sex/age/nationality) are served from /data/<slug>/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .common import http_get, read_json, safe_reason, sha256_bytes, utcnow, write_json

BASE = "https://app-navi.biz/maps/population"
SITE_INTERVAL = 1.5
SLUGS = ["hokkaido", "tohoku", "kanto", "koshinetsu", "chubu_hokuriku", "kinki",
         "chugoku", "shikoku", "kyushu_okinawa"]
LANGS = ["ja", "en", "de", "es", "fr"]
ATTRS = ["sex", "age", "nationality"]


def page_url(lang: str, slug: str) -> str:
    return f"{BASE}/{lang}/{slug}.html"


def extract_js_object(html: str, name: str):
    m = re.search(r"const " + re.escape(name) + r"\s*=\s*(\{.*)", html)
    if not m:
        return None
    return json.JSONDecoder().raw_decode(m.group(1))[0]


def extract_slug_map(html: str) -> dict:
    m = re.search(r"const SLUG_MAP\s*=\s*\{([^}]*)\}", html)
    if not m:
        return {}
    return dict(re.findall(r"'(\d{2})'\s*:\s*'([a-z_]+)'", m.group(1)))


def canonical_numbers(pop_data: dict) -> dict:
    """Language-independent numeric content: {(year,dir,code): (total, top10 pairs)}."""
    out = {}
    for year, dirs in pop_data.items():
        for direction, cities in dirs.items():
            for code, rec in cities.items():
                out[f"{year}|{direction}|{code}"] = [
                    rec.get("total"),
                    [[t.get("code"), t.get("value")] for t in rec.get("top10", [])],
                ]
    return out


def fetch_snapshot(out_dir: Path) -> dict:
    """Fetch all regional pages (all languages) and attribute JSON; write snapshot."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"retrieved_at": utcnow(), "pages": [], "attributes": [], "failures": [],
                "language_consistency": [], "slug_map": {}}
    ja_numbers = {}
    for slug in SLUGS:
        for lang in LANGS:
            url = page_url(lang, slug)
            try:
                body, _, attempts = http_get(url, host_key="site", min_interval=SITE_INTERVAL)
                html = body.decode("utf-8", "ignore")
                pop = extract_js_object(html, "POP_DATA")
                if pop is None:
                    raise ValueError("POP_DATA not found")
                nums = canonical_numbers(pop)
                row = {"url": url, "slug": slug, "lang": lang, "bytes": len(body),
                       "sha256": sha256_bytes(body), "cities": len(nums),
                       "years": sorted(pop.keys())}
                manifest["pages"].append(row)
                if lang == "ja":
                    ja_numbers[slug] = nums
                    write_json(out_dir / f"popdata_{slug}.json.gz", pop, gz=True)
                    manifest["slug_map"] = manifest["slug_map"] or extract_slug_map(html)
                else:
                    base = ja_numbers.get(slug)
                    if base is not None:
                        diff = [k for k in set(base) | set(nums) if base.get(k) != nums.get(k)]
                        manifest["language_consistency"].append({
                            "slug": slug, "lang": lang, "compared": len(base),
                            "differences": len(diff), "examples": sorted(diff)[:5]})
            except Exception as exc:  # noqa: BLE001
                manifest["failures"].append({"url": url, "stage": "page", "target": f"{slug}/{lang}",
                                             "exception_type": type(exc).__name__,
                                             "reason": safe_reason(exc), "retry_count": 3})
    for slug in SLUGS:
        for attr in ATTRS:
            url = f"{BASE}/data/{slug}/{attr}.json"
            try:
                body, _, attempts = http_get(url, host_key="site", min_interval=SITE_INTERVAL)
                obj = json.loads(body.decode("utf-8"))
                write_json(out_dir / f"attr_{slug}_{attr}.json.gz", obj, gz=True)
                manifest["attributes"].append({"url": url, "slug": slug, "attr": attr,
                                               "bytes": len(body), "sha256": sha256_bytes(body),
                                               "years": sorted(obj.keys())})
            except Exception as exc:  # noqa: BLE001
                manifest["failures"].append({"url": url, "stage": "attribute", "target": f"{slug}/{attr}",
                                             "exception_type": type(exc).__name__,
                                             "reason": safe_reason(exc), "retry_count": 3})
    write_json(out_dir / "manifest.json", manifest)
    return manifest


class PublicData:
    """In-memory view of a snapshot: lookups by (year, direction, code)."""

    def __init__(self, snap_dir: Path):
        self.snap_dir = Path(snap_dir)
        mp = self.snap_dir / "manifest.json"
        self.available = mp.exists()
        self.manifest = read_json(mp) if self.available else {"missing": True, "failures": []}
        self.pop = {}        # (year, dir, code) -> record
        self.attr = {}       # (attr, year, dir, sub, code) -> record
        self.slug_of = {}    # code -> slug where it was found
        for slug in SLUGS:
            p = self.snap_dir / f"popdata_{slug}.json.gz"
            if p.exists():
                for year, dirs in read_json(p).items():
                    for direction, cities in dirs.items():
                        for code, rec in cities.items():
                            self.pop[(year, direction, code)] = rec
                            self.slug_of[code] = slug
            for a in ATTRS:
                p = self.snap_dir / f"attr_{slug}_{a}.json.gz"
                if p.exists():
                    for year, dirs in read_json(p).items():
                        for direction, subs in dirs.items():
                            for sub, cities in subs.items():
                                for code, rec in cities.items():
                                    self.attr[(a, year, direction, sub, code)] = rec

    def codes(self, year: str, direction: str):
        return sorted(c for (y, d, c) in self.pop if y == year and d == direction)
