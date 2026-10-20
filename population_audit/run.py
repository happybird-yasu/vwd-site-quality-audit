"""CLI: snapshot | year | merge.

Overnight-safe: an individual failure (file, sheet, listing, public fetch) is
recorded with URL/target/reason/retry count and processing continues. Failures,
deferred work and unchecked scope are reported separately from matches and
mismatches; nothing is zero-filled or silently swallowed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

from . import official as off
from .common import log_failure, read_json, safe_reason, utcnow, write_json
from .compare import Tally, compare_sheet, public_internal_checks
from .public import PublicData, fetch_snapshot

MAX_FILE_RETRIES = 2   # whole-file attempts across runs (each download itself retries 3x)


def cmd_snapshot(a):
    try:
        m = fetch_snapshot(Path(a.out))
        bad = [c for c in m["language_consistency"] if c["differences"]]
        print(f"pages={len(m['pages'])} attributes={len(m['attributes'])} "
              f"failures={len(m['failures'])} language_diffs={len(bad)}")
    except Exception as exc:  # noqa: BLE001  (recorded; downstream marks scope unchecked)
        log_failure(Path(a.out), "public-snapshot", "snapshot", exc, 0)
        print(f"snapshot crashed: {safe_reason(exc)}")
    return 0


def _state_path(work: Path, item: dict) -> Path:
    return work / "state" / f"{item['year']}_{item['kind']}_{item['stat_inf_id']}.json"


def _process_item(item, pub, work: Path):
    meta = off.download(item, work / "official")
    try:
        book = off.Book(Path(meta["path"]))
    except Exception:  # noqa: BLE001  unreadable cached file: drop it and fetch once more
        if not meta.get("cached"):
            raise
        Path(meta["path"]).unlink()
        meta = off.download(item, work / "official")
        book = off.Book(Path(meta["path"]))
    tally = Tally()
    totals, sheet_failures = {}, []
    sheets = 0
    try:
        for name in book.sheetnames:
            if not name[:5].isdigit():
                continue
            try:
                sh = off.parse_sheet(name, book.rows(name), item["kind"])
                if sh.direction and sh.direction != item["direction_assumed"]:
                    raise off.LayoutError(
                        f"title says {sh.direction} but list order implies {item['direction_assumed']}")
                totals[sh.dest_code] = {"total": sh.total_row["vals"].get(off.COL_ALL),
                                        "row": sh.total_row["row"]}
                compare_sheet(sh, str(item["year"]), item["direction_assumed"], pub, tally,
                              item["stat_inf_id"])
                sheets += 1
            except Exception as exc:  # noqa: BLE001
                sheet_failures.append({"sheet": name, "exception_type": type(exc).__name__,
                                       "reason": safe_reason(exc)})
    finally:
        book.close()
    if sheets == 0:
        raise off.LayoutError(f"no sheet could be processed ({len(sheet_failures)} sheet failures)")
    return {"item": {k: v for k, v in meta.items() if k != "path"}, "sheets": sheets,
            "sheet_failures": sheet_failures, "counts": tally.as_dict(),
            "details": tally.details, "truncated": tally.truncated, "official_totals": totals}


def cmd_year(a):
    year = int(a.year)
    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pub = PublicData(Path(a.snapshot))
    summary = {"year": year, "started_at": utcnow(), "files": [], "listing_warnings": [],
               "listings": [], "discovery_failures": [], "status_counts": {},
               "public_snapshot_available": pub.available}
    t0 = time.time()
    deadline = t0 + a.deadline_minutes * 60 if a.deadline_minutes else None
    for kind in ("nationality", "agesex"):
        try:
            lst = off.discover(year, kind)
        except Exception as exc:  # noqa: BLE001
            url = off.LIST_URL.format(tclass=off.TCLASS[kind], year=year)
            log_failure(out, f"{year}/{kind}", "discover", exc, 3, url=url)
            summary["discovery_failures"].append({"kind": kind, "url": url,
                                                  "reason": safe_reason(exc)})
            continue
        summary["listing_warnings"] += [f"{kind}: {w}" for w in lst.warnings]
        summary["listings"].append({"kind": kind, "url": lst.url, "html_sha256": lst.html_sha256,
                                    "entries": len(lst.items)})
        items = lst.items
        if a.max_prefectures:
            items = [i for i in items if int(i["pref_code"]) <= a.max_prefectures]
        if a.only_pref:
            items = [i for i in items if i["pref_code"] in a.only_pref.split(",")]
        for item in items:
            sp = _state_path(work, item)
            res_path = work / "results" / f"{item['year']}_{item['kind']}_{item['stat_inf_id']}.json.gz"
            base = {"target": sp.stem, "year": year, "kind": kind, "pref_code": item["pref_code"],
                    "direction": item["direction_assumed"], "url": item["url"]}
            st = read_json(sp) if sp.exists() else {"status": "pending", "retries": 0}
            if st["status"] == "completed" and res_path.exists():
                summary["files"].append({**base, **st, "resumed": True})
                continue
            if st.get("retries", 0) >= MAX_FILE_RETRIES:
                summary["files"].append({**base, "status": "failed", "retries": st["retries"],
                                         "reason": "retry limit reached; " + st.get("reason", "")})
                continue
            if deadline and time.time() > deadline:
                summary["files"].append({**base, "status": "deferred_deadline", "retries": st.get("retries", 0)})
                continue
            write_json(sp, {**st, "status": "in_progress", "started_at": utcnow()})
            try:
                result = _process_item(item, pub, work)
                write_json(res_path, result, gz=True)
                partial = bool(result["sheet_failures"])
                for sf in result["sheet_failures"]:
                    log_failure(out, f"{sp.stem}#{sf['sheet']}", "sheet", RuntimeError(sf["reason"]),
                                st.get("retries", 0), url=item["url"])
                st = {"status": "partial" if partial else "completed", "finished_at": utcnow(),
                      "retries": st.get("retries", 0) + (1 if partial else 0),
                      "sheets": result["sheets"], "sheet_failures": len(result["sheet_failures"]),
                      "sha256": result["item"].get("sha256")}
            except Exception as exc:  # noqa: BLE001
                retries = st.get("retries", 0) + 1
                log_failure(out, sp.stem, "process", exc, retries, url=item["url"])
                st = {"status": "failed", "retries": retries, "exception_type": type(exc).__name__,
                      "reason": safe_reason(exc), "finished_at": utcnow()}
            write_json(sp, st)
            summary["files"].append({**base, **st})
            print(f"[{year}] {sp.stem} pref={item['pref_code']} {item['direction_assumed']} -> "
                  f"{st['status']} ({time.time() - t0:.0f}s)", flush=True)
    try:
        _finalize_year(year, work, out, pub, summary)
    except Exception as exc:  # noqa: BLE001
        summary["finalize_error"] = safe_reason(exc)
        log_failure(out, f"{year}/finalize", "finalize", exc, 0)
        write_json(out / f"year_{year}_summary.json", summary)
    return 0


def _finalize_year(year, work, out, pub, summary):
    tally = Tally()
    details, totals = [], {}
    for p in sorted((work / "results").glob(f"{year}_*.json.gz")):
        try:
            r = read_json(p)
        except Exception as exc:  # noqa: BLE001
            log_failure(out, p.name, "read_result", exc, 0)
            continue
        tally.merge_counts(r["counts"])
        details += r["details"]
        k, d = r["item"]["kind"], r["item"]["direction_assumed"]
        for code, v in r["official_totals"].items():
            totals.setdefault((code, d), {})[k] = v["total"]
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
    not_done = [f for f in summary["files"] if f["status"] != "completed"]
    summary["complete"] = not not_done and not summary["discovery_failures"]
    write_json(out / f"year_{year}_summary.json", summary)
    write_json(out / f"year_{year}_rerun_targets.json", {
        "year": year, "generated_at": utcnow(),
        "files": [{k: f.get(k) for k in ("target", "kind", "pref_code", "direction", "url",
                                         "status", "retries", "reason")} for f in not_done],
        "discovery_failures": summary["discovery_failures"],
        "how_to_rerun": "workflow_dispatch with years=[%d]; completed files are cached and skipped" % year,
    })
    with gzip.open(out / f"year_{year}_details.jsonl.gz", "wt", encoding="utf-8") as f:
        for row in details:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"year": year, "status_counts": statuses, "complete": summary["complete"]},
                     ensure_ascii=False))


def cmd_merge(a):
    from .report import build_report
    build_report(Path(a.inputs), Path(a.out), Path(a.snapshot) if a.snapshot else None,
                 expected_years=[y for y in a.expected_years.split(",") if y],
                 test_result=a.test_result)
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
    y.add_argument("--deadline-minutes", type=float, default=0,
                   help="stop starting new files after this many minutes; rest is deferred")
    y.set_defaults(fn=cmd_year)
    m = sub.add_parser("merge")
    m.add_argument("--inputs", required=True); m.add_argument("--out", required=True)
    m.add_argument("--snapshot"); m.add_argument("--expected-years", default="")
    m.add_argument("--test-result", default="unknown"); m.set_defaults(fn=cmd_merge)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
