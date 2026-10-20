"""Numeric comparison of public-site values against official table values.

Statuses: match | mismatch | public_missing | official_missing | not_comparable.
A suppressed/blank official cell is never treated as 0 (-> not_comparable).
"""
from __future__ import annotations

from collections import defaultdict

from .official import (COL_ALL, FEMALE_TOTAL, MALE_TOTAL, NAT_COLS, PUBLIC_AGE_BUCKETS,
                       col_letter, is_aggregate_code)

DETAIL_CAP = 300_000


class Tally:
    def __init__(self):
        self.counts = defaultdict(lambda: defaultdict(int))
        self.details = []
        self.truncated = False

    def add(self, check: str, status: str, detail: dict | None = None):
        self.counts[check]["tested"] += 1
        self.counts[check][status] += 1
        if status != "match" and detail is not None:
            if len(self.details) < DETAIL_CAP:
                self.details.append({"check": check, "status": status, **detail})
            else:
                self.truncated = True

    def merge_counts(self, other: dict):
        for check, c in other.items():
            for k, v in c.items():
                self.counts[check][k] += v

    def as_dict(self):
        return {k: dict(v) for k, v in self.counts.items()}


def _cmp(tally, check, pub, off, base):
    if off is None:
        tally.add(check, "not_comparable", {**base, "public": pub, "official": None})
    elif pub is None:
        tally.add(check, "public_missing", {**base, "public": None, "official": off})
    elif pub == off:
        tally.add(check, "match")
    else:
        tally.add(check, "mismatch", {**base, "public": pub, "official": off, "diff": pub - off})


def _off_vals(sh, cols):
    """Sum official columns of the total row; None if any is non-numeric."""
    t = sh.total_row["vals"]
    vs = [t.get(c) for c in cols]
    return None if any(v is None for v in vs) else sum(vs)


def official_attr_columns(kind: str):
    """attribute -> sub -> official column list (all-sexes block unless noted)."""
    if kind == "nationality":
        return {"nationality": {"japanese": [NAT_COLS["japanese"]], "foreign": [NAT_COLS["foreign"]]}}
    sub_age = {k: list(v) for k, v in PUBLIC_AGE_BUCKETS.items()}
    return {"sex": {"male": [MALE_TOTAL], "female": [FEMALE_TOTAL]}, "age": sub_age}


def compare_sheet(sh, year: str, direction: str, pub, tally: Tally, file_id: str):
    code = sh.dest_code
    if is_aggregate_code(code):
        tally.add("sheet_skipped_aggregate_destination", "match")
        return
    base0 = {"year": year, "direction": direction, "code": code, "sheet": sh.name,
             "file": file_id}
    tally.add("official_internal_identities", "mismatch" if sh.integrity else "match",
              {**base0, "attribute": "identity", "public": None, "official": None,
               "notes": "; ".join(sh.integrity)} if sh.integrity else None)
    tr = sh.total_row
    tcell = f"{col_letter(COL_ALL)}{tr['row']}"
    if sh.kind == "nationality":
        pr = pub.pop.get((year, direction, code))
        off_total = tr["vals"].get(COL_ALL)
        base = {**base0, "attribute": "total", "name": (pr or {}).get("name"),
                "cell": tcell, "official_sum_all_numeric_rows": sh.sum_all_numeric}
        pub_total = pr.get("total") if pr else None
        if pr is not None and pub_total == sh.sum_all_numeric and off_total != pub_total:
            base["notes"] = "public value equals the sum of ALL numeric rows (subtotal double count)"
        _cmp(tally, "total_vs_official", pub_total, off_total, base)
        if pr is not None:
            _top10(sh, year, direction, "total", pr.get("top10", []), [COL_ALL], tally, base0)
    for attr, subs in official_attr_columns(sh.kind).items():
        for sub, cols in subs.items():
            rec = pub.attr.get((attr, year, direction, sub, code))
            off = _off_vals(sh, cols)
            letters = "+".join(f"{col_letter(c)}{tr['row']}" for c in cols)
            base = {**base0, "attribute": f"{attr}:{sub}", "name": (rec or {}).get("name"),
                    "cell": letters}
            _cmp(tally, f"{attr}_{sub}_vs_official", rec.get("total") if rec else None, off, base)
            if rec is not None:
                _top10(sh, year, direction, f"{attr}:{sub}", rec.get("top10", []), cols, tally, base0)


def _top10(sh, year, direction, attribute, top10, cols, tally, base0):
    check = f"top10_values_{attribute.replace(':', '_')}"
    prev = None
    seen = set()
    for i, entry in enumerate(top10):
        oc = entry.get("code")
        base = {**base0, "attribute": attribute, "rank": i + 1, "origin_code": oc,
                "name": entry.get("name")}
        row = sh.records.get(oc)
        if row is None:
            tally.add(check, "official_missing", {**base, "public": entry.get("value"),
                                                  "official": None})
        else:
            vals = [row["vals"].get(c) for c in cols]
            off = None if any(v is None for v in vals) else sum(vals)
            base["cell"] = "+".join(f"{col_letter(c)}{row['row']}" for c in cols)
            _cmp(tally, check, entry.get("value"), off, base)
        if oc in seen:
            tally.add("top10_duplicate_origin", "mismatch", {**base, "public": oc, "official": None})
        else:
            tally.add("top10_duplicate_origin", "match")
        seen.add(oc)
        if oc and is_aggregate_code(oc):
            tally.add("top10_contains_aggregate_row", "mismatch",
                      {**base, "public": oc, "official": None})
        else:
            tally.add("top10_contains_aggregate_row", "match")
        v = entry.get("value")
        if prev is not None and v is not None and v > prev:
            tally.add("top10_sorted_descending", "mismatch", {**base, "public": v, "official": prev})
        else:
            tally.add("top10_sorted_descending", "match")
        prev = v if v is not None else prev


def public_internal_checks(pub, years, tally: Tally):
    """Checks using only public data: attribute sums vs total, direction reuse."""
    groups = {"sex": ["male", "female"], "nationality": ["japanese", "foreign"],
              "age": ["0-19", "20-39", "40-59", "60+"]}
    for year in years:
        for direction in ("inflow", "outflow"):
            for code in pub.codes(year, direction):
                total = pub.pop[(year, direction, code)].get("total")
                for attr, subs in groups.items():
                    vals = [pub.attr.get((attr, year, direction, s, code)) for s in subs]
                    if any(v is None for v in vals):
                        tally.add(f"public_sum_{attr}_vs_total", "not_comparable",
                                  {"year": year, "direction": direction, "code": code,
                                   "attribute": attr, "public": None, "official": None,
                                   "notes": "attribute record missing"})
                        continue
                    s = sum(v.get("total") or 0 for v in vals)
                    # age buckets exclude 不詳/その他, so a small deficit is expected there.
                    tally.add(f"public_sum_{attr}_vs_total", "match" if s == total else "mismatch",
                              {"year": year, "direction": direction, "code": code,
                               "attribute": attr, "public": s, "official": None,
                               "notes": f"public total {total}; sum of sub-values {s}"})
        for attr, subs in groups.items():
            for sub in subs:
                for code in pub.codes(year, "inflow"):
                    a = pub.attr.get((attr, year, "inflow", sub, code))
                    b = pub.attr.get((attr, year, "outflow", sub, code))
                    if a is None or b is None:
                        continue
                    same = (a.get("total") == b.get("total")
                            and a.get("top10") == b.get("top10"))
                    tally.add(f"direction_reuse_{attr}", "mismatch" if same else "match",
                              {"year": year, "direction": "both", "code": code,
                               "attribute": f"{attr}:{sub}", "public": a.get("total"),
                               "official": None,
                               "notes": "inflow and outflow records are identical"})
