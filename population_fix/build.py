"""Regenerate map data from the official reference tables (第1-4表).

Rules (see rules.py): the 総数 row (00000) is read once and used as the municipality
total; subtotal/breakdown rows are never summed.  Suppressed / non-numeric cells are
never turned into 0 - the affected record is omitted and listed in `issues`.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from population_audit import official as off
from population_audit.common import safe_reason

from .rules import (AGE_UNKNOWN_COL, SUB_COLUMNS, TIER, TOP_N, TOTAL_COL, classify_origin,
                    is_ranked_unit)

NAT_SUBS = [("nationality", "japanese"), ("nationality", "foreign")]
AGE_SUBS = [("sex", "male"), ("sex", "female"), ("age", "0-19"), ("age", "20-39"),
            ("age", "40-59"), ("age", "60+")]
AGGREGATE_KINDS = ("city_total", "special_wards_total", "other_wards", "other_municipalities")


def _sum_cols(vals: dict, cols):
    vs = [vals.get(c) for c in cols]
    return None if any(v is None for v in vs) else sum(vs)


def _rank(units, cols):
    """Top-N ranked units by the sum of `cols`; also count origin rows hidden by non-numeric cells."""
    ranked, hidden = [], 0
    for code, vals in units:
        v = _sum_cols(vals, cols)
        if v is None:
            hidden += 1
        elif v > 0:
            ranked.append((v, code))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [(c, v) for v, c in ranked[:TOP_N]], hidden, len(ranked)


def extract_file(path: Path, year: int, kind: str, direction: str) -> dict:
    """Parse one official workbook into per-destination facts (rankings already selected)."""
    book = off.Book(path)
    dests, failures = {}, []
    try:
        for name in book.sheetnames:
            if not name[:5].isdigit():
                continue
            try:
                sh = off.parse_sheet(name, book.rows(name), kind)
                if sh.direction and sh.direction != direction:
                    raise off.LayoutError(f"title says {sh.direction}, expected {direction}")
                dests[sh.dest_code] = _extract_sheet(sh, name, kind)
            except Exception as exc:  # noqa: BLE001
                failures.append({"sheet": name, "reason": safe_reason(exc)})
    finally:
        book.close()
    return {"year": year, "kind": kind, "direction": direction, "path": str(path),
            "dests": dests, "failures": failures}


def normalize_extract(ex: dict) -> dict:
    """JSON turns integer column keys into strings; restore them after loading."""
    for d in ex["dests"].values():
        d["vals"] = {int(k): v for k, v in d["vals"].items()}
    return ex


def _extract_sheet(sh, sheet_name: str, kind: str) -> dict:
    pref_rows = {c: r["vals"].get(TOTAL_COL) for c, r in sh.records.items()
                 if c.endswith("000") and c != "00000"}
    units, excluded = [], Counter()
    excl_max = defaultdict(int)
    for code, rec in sh.records.items():
        if code == sh.dest_code:
            continue
        k = classify_origin(code, rec.get("label", ""))
        if k == "individual":
            units.append((code, rec["vals"], rec.get("label", "")))
        else:
            excluded[k] += 1
            v = rec["vals"].get(TOTAL_COL)
            if k in AGGREGATE_KINDS and isinstance(v, int):
                excl_max[k] = max(excl_max[k], v)
    unit_vals = [(c, v) for c, v, _ in units]
    unit_codes = {int(c) for c, _, _ in units}
    absent = []
    for code, rec in sh.records.items():
        if classify_origin(code, rec.get("label", "")) in ("city_total", "special_wards_total"):
            lo = int(code)
            nxt = min([int(x) for x in off.DESIGNATED_CITY_CODES if x[:2] == code[:2] and int(x) > lo]
                      + [int(code[:2] + "200")])
            v = rec["vals"].get(TOTAL_COL)
            if isinstance(v, int) and not any(lo < u < nxt for u in unit_codes):
                absent.append((code, rec.get("label", ""), v))   # a whole city with no ward listed
    absent.sort(key=lambda x: -x[2])
    tv = sh.total_row["vals"]
    out = {"sheet": sheet_name, "sheet_name_text": sheet_name[5:], "total_row": sh.total_row["row"],
           "pref_rows": pref_rows,
           "vals": dict(tv), "origin_labels": {c: lab for c, _, lab in units},
           "excluded_rows": dict(excluded), "excluded_max": dict(excl_max),
           "absent_cities": absent[:3], "absent_city_max": absent[0][2] if absent else 0,
           "non_numeric_cells": len(sh.non_numeric), "integrity": list(sh.integrity),
           "ranks": {}, "hidden_origin": {}, "ranked_candidates": {}}
    keys = [("total", [TOTAL_COL])] + [(f"{a}:{s}", SUB_COLUMNS[(a, s)])
                                       for a, s in (NAT_SUBS if kind == "nationality" else AGE_SUBS)]
    for key, cols in keys:
        top, hidden, n = _rank(unit_vals, cols)
        out["ranks"][key] = top
        out["hidden_origin"][key] = hidden
        out["ranked_candidates"][key] = n
    return out


def merge_extracts(extracts: list):
    """Merge per-file extracts that share (year, direction, kind) - one per prefecture.

    Destination codes from different files are unioned; a code seen in two files is
    kept from the first (sorted by path) and reported, never silently overwritten.
    """
    merged, duplicates = {}, []
    for e in sorted(extracts, key=lambda x: str(x["path"])):
        k = (e["year"], e["direction"], e["kind"])
        m = merged.setdefault(k, {"year": e["year"], "direction": e["direction"],
                                  "kind": e["kind"], "dests": {}, "failures": [], "paths": []})
        for code, d in e["dests"].items():
            if code in m["dests"]:
                duplicates.append({"year": e["year"], "direction": e["direction"],
                                   "kind": e["kind"], "code": code, "file": str(e["path"])})
                continue
            m["dests"][code] = d
        m["failures"] += e["failures"]
        m["paths"].append(str(e["path"]))
    return merged, duplicates


def assemble(extracts: list, public_names: dict | None = None) -> dict:
    """Combine per-file extracts into POP_DATA / attribute structures + issue lists."""
    public_names = public_names or {}
    sheet_names = defaultdict(dict)          # year -> code -> "東京都新宿区"
    agg_names = defaultdict(dict)            # year -> aggregate code -> "静岡県浜松市"
    for e in extracts:
        for code, d in e["dests"].items():
            tgt = sheet_names if classify_origin(code, "") == "individual" else agg_names
            tgt[e["year"]].setdefault(code, d["sheet_name_text"])
    pop = defaultdict(lambda: defaultdict(dict))
    attr = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(dict))))
    age_unknown = defaultdict(lambda: defaultdict(dict))
    issues = {"suppressed_record": [], "name_fallback": [], "cross_table_total_mismatch": [],
              "integrity": [], "ranking_incomplete": [], "sheet_failures": [], "file_failures": [],
              "duplicate_destination": []}
    name_fallbacks = set()

    def qualify(year, code, n):
        """Official sheets name some wards without prefecture/city (e.g. 2025 浜松市の区): add them."""
        pref = off.PREF_NAMES[int(code[:2]) - 1]
        if n.startswith(pref):
            return n
        parents = [dc for dc in off.DESIGNATED_CITY_CODES
                   if dc[:2] == code[:2] and dc < code and int(code[2:]) < 200]
        ptxt = agg_names[year].get(max(parents)) if parents else None
        if ptxt and ptxt.startswith(pref):
            return ptxt + n
        issues["name_fallback"].append({"year": year, "code": code, "name": pref + n,
                                        "reason": "parent designated-city name not found"})
        return pref + n

    def name_of(year, code, label=""):
        n = public_names.get(code) or sheet_names[year].get(code)
        if n:
            return qualify(year, code, n)
        if not n:
            n = label or code
            if (year, code) not in name_fallbacks:
                name_fallbacks.add((year, code))
                issues["name_fallback"].append({"year": year, "code": code, "name": n})
        return n

    def ranking(year, ex, key):
        return [{"code": c, "name": name_of(year, c, ex["origin_labels"].get(c, "")), "value": v}
                for c, v in ex["ranks"][key]]

    by_key, dups = merge_extracts(extracts)
    issues["duplicate_destination"] = dups
    for (year, direction, kind), e in sorted(by_key.items()):
        for f in e["failures"]:
            issues["sheet_failures"].append({"year": year, "direction": direction, "kind": kind, **f})
        for code, d in sorted(e["dests"].items()):
            if classify_origin(code, "") != "individual":
                continue
            if d["integrity"]:
                issues["integrity"].append({"year": year, "direction": direction, "code": code,
                                            "kind": kind, "notes": d["integrity"]})
            for key, n in d["hidden_origin"].items():
                if n:
                    issues["ranking_incomplete"].append({"year": year, "direction": direction,
                                                         "code": code, "attribute": key,
                                                         "origin_rows_hidden": n})
            nm = name_of(year, code)
            if kind == "nationality":
                tot = d["vals"].get(TOTAL_COL)
                if tot is None:
                    issues["suppressed_record"].append({"year": year, "direction": direction,
                                                        "code": code, "attribute": "total"})
                else:
                    pop[str(year)][direction][code] = {"name": nm, "total": tot,
                                                       "top10": ranking(year, d, "total")}
                for a, s in NAT_SUBS:
                    v = _sum_cols(d["vals"], SUB_COLUMNS[(a, s)])
                    if v is None:
                        issues["suppressed_record"].append({"year": year, "direction": direction,
                                                            "code": code, "attribute": f"{a}:{s}"})
                        continue
                    attr[a][str(year)][direction][s][code] = {"name": nm, "total": v,
                                                              "top10": ranking(year, d, f"{a}:{s}")}
            else:
                for a, s in AGE_SUBS:
                    v = _sum_cols(d["vals"], SUB_COLUMNS[(a, s)])
                    if v is None:
                        issues["suppressed_record"].append({"year": year, "direction": direction,
                                                            "code": code, "attribute": f"{a}:{s}"})
                        continue
                    attr[a][str(year)][direction][s][code] = {"name": nm, "total": v,
                                                              "top10": ranking(year, d, f"{a}:{s}")}
                unk, allv = d["vals"].get(AGE_UNKNOWN_COL), d["vals"].get(TOTAL_COL)
                if unk is not None and allv is not None:
                    age_unknown[str(year)][direction][code] = {"unknown_other": unk,
                                                               "age_total_incl_unknown": allv}
                nat = by_key.get((year, direction, "nationality"))
                if nat and code in nat["dests"]:
                    a_t, n_t = d["vals"].get(TOTAL_COL), nat["dests"][code]["vals"].get(TOTAL_COL)
                    if a_t is not None and n_t is not None and a_t != n_t:
                        issues["cross_table_total_mismatch"].append(
                            {"year": year, "direction": direction, "code": code,
                             "agesex_total": a_t, "nationality_total": n_t})
    return {"pop": _plain(pop), "attr": _plain(attr), "age_unknown": _plain(age_unknown),
            "issues": issues, "tier": TIER}


def _plain(o):
    if isinstance(o, defaultdict) or isinstance(o, dict):
        return {k: _plain(v) for k, v in sorted(o.items())}
    return o


def region_subset(data: dict, slug: str, code_slug) -> dict:
    """Restrict {year:{dir:{code:rec}}} to destination codes of one regional page."""
    out = {}
    for year, dirs in data.items():
        out[year] = {d: {c: r for c, r in cities.items() if code_slug(c) == slug}
                     for d, cities in dirs.items()}
    return out
