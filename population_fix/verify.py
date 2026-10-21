"""Independent acceptance check of GENERATED files against the official workbooks.

Deliberately does not import build.py or population_audit.official's parser: it reads
workbooks with openpyxl/xlrd directly, scans column I for the 00000 row, and recomputes
top-10 rankings with its own small implementation of the documented rule.
It reads the generated JSON/HTML from disk (not in-memory objects).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DESIGNATED = {"01100", "04100", "11100", "12100", "13100", "14100", "14130", "14150", "15100",
              "22100", "22130", "23100", "26100", "27100", "27140", "28100", "33100", "34100",
              "40100", "40130", "43100"}
CODE = re.compile(r"^\d{5}$")
SUB_COLS = {("nationality", "japanese"): [10], ("nationality", "foreign"): [11],
            ("sex", "male"): [18], ("sex", "female"): [27], ("age", "0-19"): [10, 11],
            ("age", "20-39"): [12, 13], ("age", "40-59"): [14, 15], ("age", "60+"): [16]}


def _sheets(path: Path):
    """Yield (sheet_name, rows list) using direct library reads."""
    if str(path).lower().endswith(".xls"):
        import xlrd
        b = xlrd.open_workbook(str(path), on_demand=True)
        for n in b.sheet_names():
            sh = b.sheet_by_name(n)
            yield n, [tuple(sh.row_values(i)) for i in range(sh.nrows)]
        b.release_resources()
    else:
        import openpyxl
        b = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        for n in b.sheetnames:
            yield n, [tuple(r) for r in b[n].iter_rows(values_only=True)]
        b.close()


def sheet_codes(path: Path):
    """Destination codes (first five characters of sheet names) of a workbook."""
    if str(path).lower().endswith(".xls"):
        import xlrd
        b = xlrd.open_workbook(str(path), on_demand=True)
        names = list(b.sheet_names())
        b.release_resources()
    else:
        import openpyxl
        b = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        names = list(b.sheetnames)
        b.close()
    return [n[:5] for n in names if CODE.match(n[:5])]


def _num(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer() else None


def _is_dest(code: str) -> bool:
    return not code.endswith("000") and code not in DESIGNATED


def _is_ranked(code: str, label: str) -> bool:
    return (CODE.match(code) is not None and code != "00000" and not code.endswith("000")
            and code not in DESIGNATED and "その他" not in label)


def _origin_rows(rows):
    out = []
    for r in rows:
        if len(r) > 11 and r[8] is not None and CODE.match(str(r[8]).strip()):
            out.append((str(r[8]).strip(), re.sub(r"\s+", "", str(r[7])), r))
    return out


class Failures:
    def __init__(self):
        self.checks = 0
        self.items = []
        self.skipped = []
        self.by_attr = {}

    def check(self, ok: bool, **info):
        self.checks += 1
        if not ok:
            k = str(info.get("attr") or info.get("why") or "?")
            self.by_attr[k.split(":")[-1] if k.endswith(":top10_in_master") else k] = \
                self.by_attr.get(k.split(":")[-1] if k.endswith(":top10_in_master") else k, 0) + 1
        if not ok and len(self.items) < 500:
            self.items.append(info)
        elif not ok:
            self.items.append(None)
        return ok


def verify(files: list, generated: dict, master_by_year: dict | None = None) -> dict:
    """files: [{year, kind, direction, path}]; generated: {total, nationality, sex, age,
    age_unknown} JSON dicts for the destination codes to be verified (full or partial)."""
    f = Failures()
    rankings_checked = 0
    for spec in files:
        y, kind, d = str(spec["year"]), spec["kind"], spec["direction"]
        for sheet, rows in _sheets(Path(spec["path"])):
            code = sheet[:5]
            if not CODE.match(code) or not _is_dest(code):
                continue
            recs = _origin_rows(rows)
            total_row = next((r for c, _, r in recs if c == "00000"), None)
            if total_row is None:
                f.check(False, code=code, year=y, dir=d, why="no 00000 row in official sheet")
                continue
            gen_total = generated["total"].get(y, {}).get(d, {}).get(code)
            if kind == "nationality":
                f.check(gen_total is not None and gen_total["total"] == _num(total_row[9]),
                        code=code, year=y, dir=d, attr="total",
                        expected=_num(total_row[9]), actual=(gen_total or {}).get("total"))
                subs = [("nationality", "japanese"), ("nationality", "foreign")]
                groups = [("total", [9], gen_total)] + [
                    (f"{a}:{s}", SUB_COLS[(a, s)], generated[a].get(y, {}).get(d, {}).get(s, {}).get(code))
                    for a, s in subs]
            else:
                subs = [("sex", "male"), ("sex", "female"), ("age", "0-19"), ("age", "20-39"),
                        ("age", "40-59"), ("age", "60+")]
                groups = [(f"{a}:{s}", SUB_COLS[(a, s)], generated[a].get(y, {}).get(d, {}).get(s, {}).get(code))
                          for a, s in subs]
                unk = generated.get("age_unknown", {}).get(y, {}).get(d, {}).get(code)
                vals = [_num(total_row[c]) for c in range(9, 36)]
                if all(v is not None for v in vals):
                    f.check(unk is not None and unk["unknown_other"] == vals[17 - 9]
                            and unk["age_total_incl_unknown"] == vals[0],
                            code=code, year=y, dir=d, attr="age_unknown", actual=unk)
                    buckets = [(generated["age"][y][d][s][code]["total"]) for s in ("0-19", "20-39", "40-59", "60+")
                               if code in generated["age"].get(y, {}).get(d, {}).get(s, {})]
                    if len(buckets) == 4 and unk:
                        f.check(sum(buckets) + unk["unknown_other"] == vals[0], code=code, year=y,
                                dir=d, attr="age_sum_incl_unknown", actual=sum(buckets) + unk["unknown_other"],
                                expected=vals[0])
            for key, cols, rec in groups:
                vs = [_num(total_row[c]) for c in cols]
                exp = None if any(v is None for v in vs) else sum(vs)
                if exp is None:
                    f.check(rec is None, code=code, year=y, dir=d, attr=key,
                            why="official cell not numeric -> record must be omitted", actual=rec)
                    continue
                f.check(rec is not None and rec["total"] == exp, code=code, year=y, dir=d,
                        attr=key, expected=exp, actual=(rec or {}).get("total"))
                if rec is None:
                    continue
                cand = []
                for oc, lab, r in recs:
                    if oc == code or not _is_ranked(oc, lab):
                        continue
                    ov = [_num(r[c]) for c in cols]
                    if all(v is not None for v in ov) and sum(ov) > 0:
                        cand.append((-sum(ov), oc))
                cand.sort()
                want = [[oc, -v] for v, oc in cand[:10]]
                got = [[t["code"], t["value"]] for t in rec["top10"]]
                rankings_checked += len(got)
                f.check(want == got, code=code, year=y, dir=d, attr=key + ":top10",
                        expected=want[:3], actual=got[:3])
                if master_by_year is not None:
                    bad = [c for c, _ in got if c not in master_by_year.get(y, set())]
                    f.check(not bad, code=code, year=y, dir=d, attr=key + ":top10_in_master", actual=bad)
    return {"checks": f.checks, "rankings_values_checked": rankings_checked,
            "failures": [x for x in f.items if x is not None][:200],
            "failure_count": len(f.items), "failure_by_kind": f.by_attr,
            "passed": f.checks > 0 and not f.items}


def identity_checks(generated: dict) -> dict:
    """Checks on the generated data alone: splits sum to totals, direction reuse, aggregates."""
    n, bad = 0, []
    for y, dirs in generated["total"].items():
        for d, cities in dirs.items():
            for code, rec in cities.items():
                get = lambda a, s: generated[a].get(y, {}).get(d, {}).get(s, {}).get(code)  # noqa: E731
                jp, fr = get("nationality", "japanese"), get("nationality", "foreign")
                if jp and fr:
                    n += 1
                    if jp["total"] + fr["total"] != rec["total"]:
                        bad.append(("nat_sum", y, d, code))
                m, fe = get("sex", "male"), get("sex", "female")
                if m and fe:
                    n += 1
                    if m["total"] + fe["total"] != rec["total"]:
                        bad.append(("sex_sum", y, d, code))
                for t in rec["top10"]:
                    n += 1
                    cl = t["code"]
                    if cl.endswith("000") or cl in DESIGNATED or not CODE.match(cl):
                        bad.append(("aggregate_in_top10", y, d, code, cl))
                if len({t["code"] for t in rec["top10"]}) != len(rec["top10"]):
                    bad.append(("duplicate_in_top10", y, d, code))
    reuse = 0
    for a in ("sex", "age", "nationality"):
        for y in generated[a]:
            subs = generated[a][y].get("inflow", {})
            for s, cities in subs.items():
                for code, rec in cities.items():
                    other = generated[a][y].get("outflow", {}).get(s, {}).get(code)
                    if other and rec["top10"] and rec["top10"] == other["top10"]:
                        reuse += 1
    return {"checks": n, "failures": bad[:200], "failure_count": len(bad),
            "identical_inflow_outflow_rankings": reuse, "passed": not bad}


def anchors_check(generated: dict) -> dict:
    """Independent anchors supplied by the owner for 新宿区 (13104), 2025."""
    exp = {"inflow": (34561, 18622, 15939, 26303, 8258), "outflow": (38337, 20946, 17391, 26295, 12042)}
    res, ok = {}, True
    for d, (t, m, f, jp, fr) in exp.items():
        got = (generated["total"]["2025"][d]["13104"]["total"],
               generated["sex"]["2025"][d]["male"]["13104"]["total"],
               generated["sex"]["2025"][d]["female"]["13104"]["total"],
               generated["nationality"]["2025"][d]["japanese"]["13104"]["total"],
               generated["nationality"]["2025"][d]["foreign"]["13104"]["total"])
        res[d] = {"expected": exp[d], "actual": got, "match": got == exp[d]}
        ok &= got == exp[d]
    return {"anchors": res, "passed": ok}


def html_matches_data(html: str, pop_region: dict) -> bool:
    m = re.search(r"const POP_DATA\s*=\s*", html)
    if not m:
        return False
    obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
    return obj == pop_region
