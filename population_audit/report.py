"""Merge per-year results into report.json and a readable report.md."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from .common import read_json, utcnow, write_json

NOT_VERIFIED = [
    "National page (national.html): prefecture net-flow values and data cards are NOT yet "
    "compared against official prefecture-level tables in this run.",
    "Nine regional rankings as displayed (sorting/aggregation logic in the page JavaScript) are "
    "checked only through top10 values/order/aggregate-row presence, not through the UI.",
    "Foreign-national flows for 2019: compared only if the official file lists them; not "
    "assumed absent from official statistics.",
    "Languages: numeric content of regional pages is compared across ja/en/de/es/fr; localized "
    "text and number formatting are not checked.",
    "Public attribute buckets exclude 不詳/その他 age; that official column cannot be compared "
    "and is reported only through the official identity check.",
    "Suppressed/blank official cells are never converted to 0; they surface as not_comparable.",
]


def _load_details(path: Path):
    if not path.exists():
        return
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def build_report(inputs: Path, out: Path, snapshot: Path | None):
    out.mkdir(parents=True, exist_ok=True)
    years = {}
    for p in sorted(inputs.rglob("year_*_summary.json")):
        s = read_json(p)
        years[s["year"]] = {"summary": s, "details": p.with_name(p.name.replace("summary.json", "details.jsonl.gz"))}
    snap = None
    if snapshot and (snapshot / "manifest.json").exists():
        snap = read_json(snapshot / "manifest.json")
    for p in sorted(inputs.rglob("failures.jsonl")):
        pass
    report = {"generated_at": utcnow(), "years": {}, "not_verified": NOT_VERIFIED}
    lines = ["# Population-flow numeric audit", "",
             f"Generated: {report['generated_at']}", "",
             "Public side: https://app-navi.biz/maps/population/ (read-only GET).  "
             "Official side: e-Stat 住民基本台帳人口移動報告 参考表 (第1-4表).", ""]
    for year in sorted(years):
        s = years[year]["summary"]
        dd = {"subtotal_double_count_reproduced": 0}
        examples = []
        for row in _load_details(years[year]["details"]):
            if row.get("check") == "total_vs_official" and "double count" in (row.get("notes") or ""):
                dd["subtotal_double_count_reproduced"] += 1
                if len(examples) < 10:
                    examples.append(row)
        report["years"][year] = {"files": s["status_counts"], "counts": s["counts"],
                                 "listing_warnings": s["listing_warnings"], "derived": dd,
                                 "examples": examples, "detail_rows": s["detail_rows"],
                                 "detail_truncated": s["detail_truncated"]}
        lines += [f"## {year}", "", f"Official files: {s['status_counts']}",
                  f"Listing warnings: {s['listing_warnings'] or 'none'}",
                  f"Public totals equal to the all-rows sum (double count reproduced): "
                  f"{dd['subtotal_double_count_reproduced']}", "",
                  "| check | tested | match | mismatch | public_missing | official_missing | not_comparable |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for check, c in sorted(s["counts"].items()):
            lines.append(f"| {check} | {c.get('tested', 0)} | {c.get('match', 0)} | {c.get('mismatch', 0)} "
                         f"| {c.get('public_missing', 0)} | {c.get('official_missing', 0)} "
                         f"| {c.get('not_comparable', 0)} |")
        failed = [f for f in s["files"] if f.get("status") != "completed"]
        lines += ["", f"Files not completed: {len(failed)}"]
        for f in failed[:40]:
            lines.append(f"- {f.get('target')}: {f.get('status')} {f.get('exception_type', '')}")
        lines.append("")
    if snap:
        bad = [c for c in snap["language_consistency"] if c["differences"]]
        report["public_snapshot"] = {
            "retrieved_at": snap["retrieved_at"], "pages": len(snap["pages"]),
            "attribute_files": len(snap["attributes"]), "failures": snap["failures"],
            "language_pairs_compared": len(snap["language_consistency"]),
            "language_pairs_with_differences": len(bad), "language_differences": bad[:20]}
        lines += ["## Public snapshot", "",
                  f"Retrieved: {snap['retrieved_at']}; pages: {len(snap['pages'])}; "
                  f"attribute JSON: {len(snap['attributes'])}; fetch failures: {len(snap['failures'])}",
                  f"Language comparisons (vs ja): {len(snap['language_consistency'])}, "
                  f"with numeric differences: {len(bad)}", ""]
    lines += ["## Not verified / open", ""] + [f"- {x}" for x in NOT_VERIFIED] + [""]
    write_json(out / "report.json", report)
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines)[:6000])
