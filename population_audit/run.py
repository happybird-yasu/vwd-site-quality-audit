"""CLI: snapshot | year | merge.  Resumable; results saved per official file."""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

from . import official as off
from .common import log_failure, read_json, utcnow, write_json
from .compare import Tally, compare_sheet, public_internal_checks
from .public import PublicData, fetch_snapshot


def cmd_snapshot(a):
    m = fetch_snapshot(Path(a.out))
    bad = [c for c in m["language_consistency"] if c["differences"]]
    print(f"pages={len(m['pages'])} attributes={len(m['attributes'])} "
          f"failures={len(m['failures'])} language_diffs={len(bad)}")
    return 0


def _state_path(work: Path, item: dict) -> Path:
    return work / "state" / f"{item['year']}_{item['kind']}_{item['stat_inf_id']}.json"


def _process_item(item, pub, work: Path, out: Path):
    cache = work / "official"
    meta = off.download(item, cache)
    book = off.Book(Path(meta["path"]))
    tally = Tally()
    totals = {}
    sheets = checked = 0
    try:
        for name in book.sheetnames:
            if not name[:5].isdigit():
                continue
            sh = off.parse_sheet(name, book.rows(name), item["kind"])
            if sh.direction and sh.direction != item["direction_assumed"]:
                raise off.LayoutError(
                    f"{name}: title says {sh.direction} but list order implies "
                    f"{item['direction_assumed']}")
            sheets += 1
            totals[sh.dest_code] = {"total": sh.total_row["vals"].get(off.COL_ALL),
                                    "row": sh.total_row["row"]}
            compare_sheet(sh, str(item["year"]), item["direction_assumed"], pub, tally,
                          item["stat_inf_id"])
            checked += 1
    finally:
        book.close()
    return {"item": {k: v for k, v in meta.items() if k != "path"}, "sheets": sheets,
            "counts": tally.as_dict(), "details": tally.details, "truncated": tally.truncated,
            "official_totals": totals}


def cmd_year(a):
    year = int(a.year)
    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pub = PublicData(Path(a.snapshot))
    summary = {"year": year, "started_at": utcnow(), "files": [], "listing_warnings": [],
               "status_counts": {}}
    t0 = time.time()
    for kind in ("nationality", "agesex"):
        try:
            lst = off.discover(year, kind)
        except Exception as exc:  # noqa: BLE001
            log_failure(out, f"{year}/{kind}", "discover", exc, 3)
            summary["listing_warnings"].append(f"{kind}: discovery failed ({type(exc).__name__})")
            continue
        summary["listing_warnings"] += [f"{kind}: {w}" for w in lst.warnings]
        items = lst.items
        if a.max_prefectures:
            items = [i for i in items if int(i["pref_code"]) <= a.max_prefectures]
        if a.only_pref:
            items = [i for i in items if i["pref_code"] in a.only_pref.split(",")]
        summary.setdefault("listings", []).append(
            {"kind": kind, "url": lst.url, "html_sha256": lst.html_sha256, "entries": len(lst.items)})
        for item in items:
            sp = _state_path(work, item)
            res_path = work / "results" / f"{item['year']}_{item['kind']}_{item['stat_inf_id']}.json.gz"
            st = read_json(sp) if sp.exists() else {"status": "pending", "retries": 0}
            if st["status"] == "completed" and res_path.exists():
                summary["files"].append({**st, "target": sp.stem, "resumed": True})
                continue
            write_json(sp, {**st, "status": "in_progress", "started_at": utcnow()})
            try:
                result = _process_item(item, pub, work, out)
                write_json(res_path, result, gz=True)
                st = {"status": "completed", "finished_at": utcnow(), "retries": st.get("retries", 0),
                      "sheets": result["sheets"], "sha256": result["item"].get("sha256")}
            except Exception as exc:  # noqa: BLE001
                retries = st.get("retries", 0) + 1
                log_failure(out, sp.stem, "process", exc, retries)
                st = {"status": "failed", "retries": retries, "exception_type": type(exc).__name__,
                      "finished_at": utcnow()}
            write_json(sp, st)
            summary["files"].append({**st, "target": sp.stem,
                                     "pref_code": item["pref_code"], "kind": item["kind"],
                                     "direction": item["direction_assumed"]})
            print(f"[{year}] {sp.stem} pref={item['pref_code']} {item['direction_assumed']} -> "
                  f"{st['status']} ({time.time() - t0:.0f}s)", flush=True)
    _finalize_year(year, work, out, pub, summary)
    return 0


def _finalize_year(year, work, out, pub, summary):
    tally = Tally()
    details = []
    totals = {}
    for p in sorted((work / "results").glob(f"{year}_*.json.gz")):
        r = read_json(p)
        tally.merge_counts(r["counts"])
        details += r["details"]
        k = r["item"]["kind"]
        d = r["item"]["direction_assumed"]
        for code, v in r["official_totals"].items():
            totals.setdefault((code, d), {})[k] = v["total"]
    # cross-table official consistency: nationality total == age/sex total
    for (code, d), kinds in totals.items():
        if len(kinds) == 2:
            ok = kinds["nationality"] == kinds["agesex"]
            tally.add("official_cross_table_total", "match" if ok else "mismatch",
                      {"year": str(year), "direction": d, "code": code, "attribute": "total",
                       "public": None, "official": kinds["nationality"],
                       "notes": f"nationality table {kinds['nationality']} vs age/sex table {kinds['agesex']}"})
    public_internal_checks(pub, [str(year)], tally)
    details += tally.details
    statuses = {}
    for f in summary["files"]:
        statuses[f["status"]] = statuses.get(f["status"], 0) + 1
    summary["status_counts"] = statuses
    summary["finished_at"] = utcnow()
    summary["counts"] = tally.as_dict()
    summary["detail_rows"] = len(details)
    summary["detail_truncated"] = tally.truncated
    write_json(out / f"year_{year}_summary.json", summary)
    with gzip.open(out / f"year_{year}_details.jsonl.gz", "wt", encoding="utf-8") as f:
        for row in details:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"year": year, "status_counts": statuses,
                      "checks": {k: v for k, v in summary["counts"].items()}},
                     ensure_ascii=False)[:4000])


def cmd_merge(a):
    from .report import build_report
    build_report(Path(a.inputs), Path(a.out), Path(a.snapshot) if a.snapshot else None)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="population_audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_snapshot)
    y = sub.add_parser("year")
    y.add_argument("--year", required=True); y.add_argument("--snapshot", required=True)
    y.add_argument("--work", required=True); y.add_argument("--out", required=True)
    y.add_argument("--max-prefectures", type=int, default=0)
    y.add_argument("--only-pref", default="", help="comma-separated 2-digit pref codes (testing)")
    y.set_defaults(fn=cmd_year)
    m = sub.add_parser("merge")
    m.add_argument("--inputs", required=True); m.add_argument("--out", required=True)
    m.add_argument("--snapshot"); m.set_defaults(fn=cmd_merge)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
