"""National page (CITY_DATA, SEARCH_INDEX, PREF_ALL) and boundary correspondence.

CITY_DATA is replaced from the regenerated municipality data (no old numbers remain).
SEARCH_INDEX is rebuilt from the current-year (2025) municipality set.
PREF_ALL (prefecture net migration) is VERIFIED against official prefecture sheets, not
replaced; a mismatch is reported and blocks "fully complete".
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from . import render
from .rules import SLUG_MAP, region_of

CURRENT_YEAR = "2025"


def national_city_data(pop: dict) -> dict:
    """{year:{dir:{code:{name, top10}}}} - same schema the national page already uses."""
    return {y: {d: {c: {"name": r["name"], "top10": r["top10"]} for c, r in sorted(cs.items())}
                for d, cs in dd.items()} for y, dd in sorted(pop.items())}


def slug_region_names(old_index: list) -> dict:
    names = {}
    for e in old_index:
        names.setdefault(e["slug"], e["region"])
    return names


def build_search_index(pop: dict, region_names: dict) -> list:
    """Current-boundary (2025) municipalities, both directions, schema unchanged."""
    cur = {}
    for d in ("inflow", "outflow"):
        for c, r in pop.get(CURRENT_YEAR, {}).get(d, {}).items():
            cur.setdefault(c, r["name"])
    out = []
    for c in sorted(cur):
        slug = region_of(c)
        out.append({"code": c, "name": cur[c], "region": region_names.get(slug, ""), "slug": slug})
    return out


def patch_national(html: str, city_data: dict, search_index: list) -> str:
    html = render.replace_const_json(html, "CITY_DATA", city_data)
    return render.replace_const_json(html, "SEARCH_INDEX", search_index)


def pref_net_from_extracts(merged: dict) -> dict:
    """Official net migration per prefecture/year from the xx000 destination sheets.

    inflow from other prefectures = 総数 - own-prefecture row (same sheet);
    outflow to other prefectures likewise; net = inflow - outflow.
    """
    out = defaultdict(dict)
    years = {y for (y, _, k) in merged if k == "nationality"}
    for y in sorted(years):
        i, o = merged.get((y, "inflow", "nationality")), merged.get((y, "outflow", "nationality"))
        if not i or not o:
            continue
        for code, di in i["dests"].items():
            if not code.endswith("000") or code == "00000":
                continue
            do = o["dests"].get(code)
            ti, own_i = di["vals"].get(9), di.get("pref_rows", {}).get(code)
            to, own_o = (do or {}).get("vals", {}).get(9), (do or {}).get("pref_rows", {}).get(code)
            if None in (ti, own_i, to, own_o):
                out[str(y)][code] = None
            else:
                out[str(y)][code] = (ti - own_i) - (to - own_o)
    return {y: dict(v) for y, v in out.items()}


def check_pref_all(pref_all: dict, official: dict) -> dict:
    checked, mism, missing = 0, [], []
    for y, prefs in pref_all.items():
        for code, rec in prefs.items():
            off = official.get(y, {}).get(code, "absent")
            if off in ("absent", None):
                missing.append({"year": y, "code": code, "name": rec.get("name")})
                continue
            checked += 1
            if off != rec.get("net_migration"):
                mism.append({"year": y, "code": code, "name": rec.get("name"),
                             "page": rec.get("net_migration"), "official": off})
    sums = {y: sum(v for v in prefs.values() if v is not None) for y, prefs in official.items()}
    return {"checked": checked, "mismatches": mism[:100], "mismatch_count": len(mism),
            "not_checkable": missing[:100], "not_checkable_count": len(missing),
            "official_net_sum_by_year": sums,
            "passed": checked > 0 and not mism and not missing}


def geometry_codes(geojson: dict) -> set:
    codes = set()
    for f in geojson.get("features", []):
        c = (f.get("properties") or {}).get("N03_007")
        if c:
            codes.add(str(c))
    return codes


def boundary_report(pop: dict, polygon_codes: set | None) -> dict:
    """Per year/direction: data codes without a current polygon, and 2025 polygons without data."""
    rep = {"polygons_available": polygon_codes is not None, "years": {}}
    if polygon_codes is None:
        return rep
    cur_codes = set()
    for d in ("inflow", "outflow"):
        cur_codes |= set(pop.get(CURRENT_YEAR, {}).get(d, {}))
    for y, dd in sorted(pop.items()):
        codes = set()
        for d in ("inflow", "outflow"):
            codes |= set(dd.get(d, {}))
        rep["years"][y] = {"data_codes": len(codes), "without_current_polygon": len(codes - polygon_codes),
                           "without_current_polygon_examples": sorted(codes - polygon_codes)[:40]}
    rep["polygons_without_2025_data"] = len(polygon_codes - cur_codes)
    rep["polygons_without_2025_data_examples"] = sorted(polygon_codes - cur_codes)[:40]
    rep["polygon_count"] = len(polygon_codes)
    return rep
