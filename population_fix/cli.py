"""CLI.  `tokyo2025`: small-scope corrected copy.  `full`: all years/regions/languages."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

from population_audit.common import log_failure, read_json, safe_reason, utcnow

from . import build, render, verify
from .rules import SLUG_MAP, SLUGS, region_of

ATTRS = ("sex", "age", "nationality")
LANGS = ("ja", "en", "de", "es", "fr")
SITE = "site/maps/population"


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_public_names(snap: Path) -> dict:
    names = {}
    for slug in SLUGS:
        p = snap / f"popdata_{slug}.json.gz"
        if p.exists():
            for _, dirs in read_json(p).items():
                for _, cities in dirs.items():
                    for code, rec in cities.items():
                        names[code] = rec["name"]
                        for t in rec.get("top10", []):
                            names.setdefault(t["code"], t["name"])
    return names


def load_public(snap: Path, slug: str):
    pop = read_json(snap / f"popdata_{slug}.json.gz")
    attrs = {a: read_json(snap / f"attr_{slug}_{a}.json.gz") for a in ATTRS}
    return pop, attrs


def write_attr_files(out_site: Path, slug: str, attrs: dict, age_unknown: dict):
    base = out_site / "data" / slug
    for a in ATTRS:
        render.write_json(base / f"{a}.json", attrs[a])
    render.write_json(base / "age_unknown.json", age_unknown)


def cmd_tokyo2025(a):
    out = Path(a.out)
    src = Path(a.sources)
    snap = Path(a.snapshot)
    files = [{"year": 2025, "kind": "nationality", "direction": "inflow", "path": src / "13東京都_第３表_2025年.xlsx"},
             {"year": 2025, "kind": "nationality", "direction": "outflow", "path": src / "13東京都_第４表_2025年.xlsx"},
             {"year": 2025, "kind": "agesex", "direction": "inflow", "path": src / "13東京都_第１表_2025年.xlsx"},
             {"year": 2025, "kind": "agesex", "direction": "outflow", "path": src / "13東京都_第２表_2025年.xlsx"}]
    extracts = [build.extract_file(f["path"], f["year"], f["kind"], f["direction"]) for f in files]
    gen = build.assemble(extracts, load_public_names(snap))
    pop_old, attr_old = load_public(snap, "kanto")
    pref = {"13"}
    pop_new = render.merge_pop(pop_old, gen["pop"], ["2025"], pref)
    attrs_new = {x: render.merge_attr(attr_old[x], gen["attr"][x], ["2025"], pref) for x in ATTRS}
    html = Path(a.html).read_text(encoding="utf-8")
    html = render.replace_const_json(html, "POP_DATA", pop_new)
    html = render.patch_display(html)
    site = out / SITE
    (site / "ja").mkdir(parents=True, exist_ok=True)
    (site / "ja" / "kanto.html").write_text(html, encoding="utf-8")
    age_unknown = {"2025": gen["age_unknown"].get("2025", {})}
    write_attr_files(site, "kanto", attrs_new, age_unknown)
    # read back what was written and verify from disk
    written = {"total": render.read_const_json((site / "ja" / "kanto.html").read_text(encoding="utf-8"), "POP_DATA"),
               "age_unknown": json.loads((site / "data/kanto/age_unknown.json").read_text(encoding="utf-8"))}
    for x in ATTRS:
        written[x] = json.loads((site / f"data/kanto/{x}.json").read_text(encoding="utf-8"))
    tokyo_only = {k: ({y: {d: {c: r for c, r in cs.items() if c[:2] == "13"} for d, cs in dd.items()}
                       for y, dd in v.items()} if k == "total" else v) for k, v in written.items()}
    # restrict verification to Tokyo destinations (the only prefecture replaced here)
    for x in ATTRS:
        tokyo_only[x] = {y: {d: {s: {c: r for c, r in cs.items() if c[:2] == "13"} for s, cs in subs.items()}
                             for d, subs in dd.items()} for y, dd in written[x].items()}
    v = verify.verify(files, {**tokyo_only, "age_unknown": written["age_unknown"]})
    ident = verify.identity_checks({**tokyo_only, "total": {"2025": tokyo_only["total"]["2025"]},
                                    **{x: {"2025": tokyo_only[x]["2025"]} for x in ATTRS}})
    anchors = verify.anchors_check(tokyo_only)
    result = {"scope": "Tokyo (13) 2025 only - NOT nationwide", "generated_at": utcnow(),
              "verify_direct_from_excel": v, "identity": ident, "anchors": anchors,
              "html_pop_data_equals_json": verify.html_matches_data(
                  (site / "ja" / "kanto.html").read_text(encoding="utf-8"), written["total"]),
              "issues_counts": {k: len(val) for k, val in gen["issues"].items()},
              "source_hashes": {str(f["path"].name): sha(f["path"]) for f in files},
              "base_html_sha256": sha(Path(a.html)), "ranking_tier": gen["tier"]}
    render.write_json(out / "tokyo2025_verification.json", result)
    for name in ("total", "nationality", "sex", "age"):
        render.write_json(out / "json" / f"{name}.json", written[name])
    print(json.dumps({k: (val if k in ("anchors", "html_pop_data_equals_json", "scope") else
                          {kk: vv for kk, vv in val.items() if kk != "failures"} if isinstance(val, dict) else val)
                      for k, val in result.items() if k != "source_hashes"}, ensure_ascii=False, indent=1)[:3500])
    return 0 if v["passed"] and ident["passed"] and anchors["passed"] else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="population_fix")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tokyo2025")
    t.add_argument("--sources", required=True); t.add_argument("--snapshot", required=True)
    t.add_argument("--html", required=True); t.add_argument("--out", required=True)
    t.set_defaults(fn=cmd_tokyo2025)
    from .full import add_full_parser
    add_full_parser(sub)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
